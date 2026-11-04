"""One Research answer reuses exact original reads across all user questions."""

import pytest

from codey.operations.kernel_protocol import build_turn_snapshot
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.task_execution import ExecutionDelegate
from codey.policies.task_policy import TaskPolicy
from codey.research.answer_context import answer_context
from codey.research.answer_contract import answer_scope, submit_answer
from codey.research.answer_report import compile_answer_report
from codey.runtime.core.models import ToolCall
from tests.test_research_findings import TEXT, URL, execute, setup
from tests.test_research_whole_answer import record_submission
from tests.test_research_whole_kernel import run_whole


@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("native", [False, True])
def test_multiple_questions_reuse_pinned_original_without_fetch_or_adopting_claims(tmp_path, monkeypatch, native):
    store, tools, session, delegate = setup(tmp_path)
    try:
        session.task_text = "What limits writer concurrency? Can readers coexist with a writer?"
        session._memory_results["read"] = execute(delegate, tools, session, "open_url", {"url": URL})
        session.policy = TaskPolicy(session.policy.grants, strict_research=True)
        frozen = restore_snapshot(snapshot_description(build_turn_snapshot(session, native=native)))
        monkeypatch.setattr(tools.search, "fetch", lambda *_: pytest.fail("reusing a read refetched the source"))
        text = answer_context(session, tools.ledger, frozen.research_scope)
        assert TEXT in text and session.task_text in text
        args = {"state": "answered", "markdown": "Only one writer at a time [e1]. Readers and a writer may overlap [e1]."}
        result, ok, _ = ExecutionDelegate(session=session, research_tools=tools,
            research_scope=frozen.research_scope)._execute_research(ToolCall("submit_research_answer", args))
        assert ok, result.model_text
        session._memory_results["submit"] = result
        report = compile_answer_report(session, tools.ledger, store, project="", session_id="first")
        assert report.markdown == args["markdown"] and len(report.source_versions) == 1
        assert not report.finding_ids and report.selection_receipts
        assert store.index.count() == 0
    finally:
        store.close()


@pytest.mark.usefixtures("public_source_dns")
def test_omitted_window_remains_pinned_and_unread_tail_is_not_granted(tmp_path):
    store, tools, session, delegate = setup(tmp_path)
    try:
        tools.search.text = TEXT * 80 + "NEVER_READ_TAIL"
        session._memory_results["read"] = execute(delegate, tools, session, "open_url", {"url": URL, "limit": 5000})
        scope = answer_scope(session)
        frozen = restore_snapshot(snapshot_description(build_turn_snapshot(session)))
        full = answer_context(session, tools.ledger, scope, count_tokens=len, token_budget=20000)
        omitted = answer_context(session, tools.ledger, scope, count_tokens=len, token_budget=7500)
        assert "NEVER_READ_TAIL" not in full + omitted
        assert scope == answer_scope(session) and len(omitted) < len(full)
        # Rendering is a bounded view; it cannot revoke the original read.
        record_submission(session, {"state": "answered", "markdown": "Only one writer is supported [e1]."})
        assert compile_answer_report(session, tools.ledger, store, project="", session_id="first").evidence_bindings
        with pytest.raises(ValueError, match="Unknown"):
            submit_answer(scope, {"state": "answered", "markdown": "The unread tail proves it [e99]."})
        assert frozen == restore_snapshot(snapshot_description(frozen))
    finally:
        store.close()


@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("native", [False, True])
def test_two_questions_publish_with_one_fetch_and_replay_without_acquisition(tmp_path, native):
    import json

    result = run_whole(tmp_path, native=native)
    assert result.stop_reason == "done"
    before = (tmp_path / "calls.jsonl").read_bytes()
    rows = [json.loads(line) for line in before.splitlines()]
    assert sum("fetch" in row for row in rows) == sum("publication" in row for row in rows) == 1
    assert run_whole(tmp_path, native=native, resume=True) == result
    assert (tmp_path / "calls.jsonl").read_bytes() == before
