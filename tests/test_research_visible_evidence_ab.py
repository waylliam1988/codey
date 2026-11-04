"""Installed task-read citation authority, token budgeting and publication boundaries."""
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from codey.operations.task_execution import ExecutionDelegate
from codey.operations.task_guidance import task_result_context, task_working_context
from codey.providers.token_accounting import ContextBudget, RequestContextCount
from codey.research import answer_context, answer_contract
from codey.research.answer_report import AnswerReport, compile_answer_report
from codey.runtime.core.models import ToolCall, ToolResult
from tests.test_research_findings import URL, execute, setup
from tests.test_research_whole_kernel import run_whole


@pytest.mark.usefixtures("public_source_dns")
def test_previously_read_but_undisplayed_original_compiles_exactly(tmp_path):
    store, tools, session, delegate = setup(tmp_path)
    try:
        session._memory_results["r1"] = execute(delegate, tools, session, "open_url", {"url": URL})
        old = next(iter(tools.ledger.read_passages.values()))
        tools.search.text = "A changed live source must not replace the original excerpt. " * 25
        session._memory_results["r2"] = execute(delegate, tools, session, "open_url", {"url": URL})
        scope = answer_contract.answer_scope(session)
        # Force actual display pressure; both known snapshots stay citable.
        cap = len(answer_context._render_context(session, tools.ledger, scope, ())) + 10
        text = answer_context.answer_context(session, tools.ledger, scope, count_tokens=len, token_budget=cap)
        packet = next(json.loads(block) for block in text.split("\n\n") if block.startswith('{"investigation_attempts":'))
        assert [row["label"] for row in packet["previously_read_snapshots"]] == [label for label, _ in scope.aliases]
        assert len(scope.aliases) >= 2
        assert old.excerpt not in text and "previously_read_snapshots" in text
        assert "read_tool_result" not in text and "read_receipt_index" not in text
        args = {"state": "answered", "markdown": "The original permits concurrency [e1]."}
        submitted = answer_contract.submit_answer(scope, args)
        session._memory_results["s"] = ToolResult(ToolCall("submit_research_answer", args), "Recorded", ok=True,
            canonical={"research_submission": submitted})
        report = compile_answer_report(session, tools.ledger, store, project="", session_id="test")
        assert report.evidence_bindings[0]["excerpt"] == old.excerpt
        assert report.evidence_bindings[0]["passage_ref"] == old.ref
        assert len(report.source_versions) == 1
        assert AnswerReport.from_payload(report.to_payload()) == report
        # A fabricated binding still cannot survive archive reconstruction.
        payload = report.to_payload()
        payload["evidence_bindings"][0]["excerpt"] = "Fabricated text"
        with pytest.raises(ValueError):
            AnswerReport.from_payload(payload)
    finally:
        store.close()


@pytest.mark.usefixtures("public_source_dns")
def test_all_fitting_originals_are_kept_verbatim_and_invented_reads_rejected(tmp_path):
    store, tools, session, delegate = setup(tmp_path)
    try:
        session._memory_results["r1"] = execute(delegate, tools, session, "open_url", {"url": URL})
        session._memory_results["failed"] = ToolResult(ToolCall("open_url", {"url": URL + "/unread"}),
            "failed", ok=False, canonical={"read_passages": [{"ref": "p123456789abc", "start": 0, "end": 50}]})
        scope = answer_contract.answer_scope(session)
        text = answer_context.answer_context(session, tools.ledger, scope)
        assert next(iter(tools.ledger.read_passages.values())).excerpt in text
        assert scope.aliases[0][0] == "e1" and len(scope.aliases) == 1
        assert "read_tool_result" not in answer_contract.ANSWER_TOOLS
        for label in ("e2", "e99"):
            with pytest.raises(ValueError, match="Unknown"):
                answer_contract.submit_answer(scope, {"state": "answered", "markdown": f"An invented source [{label}]."})
        ref = scope.passage_refs[0]
        wrong = ExecutionDelegate(session=session, research_tools=tools, research_scope=scope)
        result, ok, _ = wrong._execute_research(ToolCall("knowledge_read", {"id": ref}))
        assert not ok and "[eN]" in result.model_text
        assert "read_receipt_index" not in result.model_text and "read_tool_result" not in result.model_text
        assert "read_tool_result" not in answer_contract.ANSWER_TOOLS
    finally:
        store.close()




@pytest.mark.usefixtures("public_source_dns")
def test_available_full_snapshot_does_not_grant_unread_tail_evidence(tmp_path):
    store, tools, session, delegate = setup(tmp_path)
    try:
        marker = "UNREAD_TAIL_FACT_92871"
        tools.search.text = "Only this introduction was read. " * 250 + marker
        session._memory_results["r"] = execute(delegate, tools, session, "open_url", {"url": URL, "limit": 500})
        scope = answer_contract.answer_scope(session)
        assert scope.aliases
        for ref in scope.passage_refs:
            passage = tools.ledger.read_passages[ref]
            assert marker in passage.source_text
            assert marker not in passage.excerpt and passage.end <= 500
        assert marker not in answer_context.answer_context(session, tools.ledger, scope)
        with pytest.raises(ValueError, match="Unknown"):
            answer_contract.submit_answer(scope, {"state": "answered", "markdown": "The unread tail says so [e99]."})
    finally:
        store.close()


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("scenario", ["answer", "repair"])
@pytest.mark.usefixtures("public_source_dns")
def test_candidate_kernel_publishes_once_and_resumes(tmp_path, native, scenario):
    result = run_whole(tmp_path, native=native, scenario=scenario)
    assert result.stop_reason == "done"
    before = (tmp_path / "calls.jsonl").read_bytes()
    assert run_whole(tmp_path, native=native, resume=True, scenario=scenario) == result
    assert (tmp_path / "calls.jsonl").read_bytes() == before
    assert sum("publication" in json.loads(line) for line in before.splitlines()) == 1


@pytest.mark.parametrize("boundary", ["provider_request_ready", "kernel_settled", "publication_committed"])
def test_candidate_cold_restart_preserves_read_snapshots_and_one_publication(tmp_path, boundary):
    source = (
        "from pathlib import Path; import sys; "
        "from tests.test_research_whole_kernel import run_whole; "
        "run_whole(Path(sys.argv[1]), boundary=sys.argv[2])"
    )
    child = subprocess.run([sys.executable, "-X", "utf8", "-c", source, str(tmp_path), boundary],
                           capture_output=True, timeout=50)
    assert child.returncode == 74, child.stderr.decode(errors="replace")
    result = run_whole(tmp_path, resume=True)
    assert result.stop_reason == "done"
    rows = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert sum("whole_request" in row for row in rows) == 2
    assert sum("publication" in row for row in rows) == 1


@pytest.mark.usefixtures("public_source_dns")
def test_work_view_uses_admitted_provider_counter_and_half_window(tmp_path):
    store, tools, session, delegate = setup(tmp_path)
    try:
        tools.search.text = "Only measured token counts may admit this original window. " * 90
        session._memory_results["read"] = execute(delegate, tools, session, "open_url", {"url": URL})
        scope = answer_contract.answer_scope(session)
        counted = []

        def count(text, *, deadline):
            counted.append((text, deadline))
            return RequestContextCount(len(text), "tokenizer")

        provider = SimpleNamespace(context_budget=ContextBudget(12000, 2000, 0, 4000), count_text=count, timeout=4.0)
        text = task_working_context(session, tools.ledger, research_scope=scope, provider=provider)
        assert len(counted) >= 2 and len(text) <= 6000
        assert len({deadline for _, deadline in counted}) == 1
        assert tools.search.text not in text and '"label":"e1"' in text
        assert answer_contract.answer_scope(session) == scope
        answer_contract.submit_answer(scope, {"state": "answered", "markdown": "The original measured window [e1]."})
    finally:
        store.close()


def test_admitted_counter_failure_never_silently_uses_an_estimate(tmp_path):
    store, tools, session, _ = setup(tmp_path)
    try:
        provider = SimpleNamespace(context_budget=ContextBudget(12000, 2000, 0, 4000), timeout=4.0,
            count_text=lambda *a, **kw: RequestContextCount(None, "unknown"))
        with pytest.raises(ValueError, match="token count is unavailable"):
            task_working_context(session, tools.ledger, research_scope=answer_contract.answer_scope(session), provider=provider)
    finally:
        store.close()


def test_no_reads_view_does_not_claim_that_originals_were_already_read(tmp_path):
    store, tools, session, _ = setup(tmp_path)
    try:
        scope = answer_contract.answer_scope(session)
        text = answer_context.answer_context(session, tools.ledger, scope)
        assert "No original excerpt has been read for this task yet" in text
        assert "Read a provided web URL with open_url" in text
        assert "Previously read snapshots are indexed below" not in text
    finally:
        store.close()


@pytest.mark.parametrize("tool", ["knowledge_search", "web_search", "source_search"])
def test_whole_answer_results_never_advertise_unavailable_receipt_recovery(tmp_path, tool):
    store, tools, session, _ = setup(tmp_path)
    try:
        session.search_results = {"r1": URL}
        session.hit_targets = {"h1": {"url": URL, "offset": 40}}
        result = ToolResult(ToolCall(tool, {}), "Complete original result data", ok=True)
        session._memory_results["exact-receipt"] = result
        text = task_result_context(session, result, answer_contract.answer_scope(session))
        assert result.model_text in text and "exact-receipt" in text
        assert "read_tool_result" not in text and "read_receipt_index" not in text
        if tool == "web_search":
            assert "open_result" in text and URL in text
        if tool == "source_search":
            assert "open_hit" in text and "offset=40" in text
        assert "read_tool_result" in task_result_context(session, result, None)
    finally:
        store.close()
