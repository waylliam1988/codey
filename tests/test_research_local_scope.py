"""Local selection uses the production kernel, gate, publication and replay."""

import hashlib
import json
import subprocess
import sys

import pytest

from codey.research.local_scope import ResearchScope, current_scope, effective_answers, registered_plan
from codey.research.passages import passage_ref
from tests.test_research_deterministic_completion import _run
from tests.test_research_findings import URL

TASK = "Explain writer limit. Explain absent measurement. Explain reader writer coexistence."
QUESTIONS = ["What limits writer concurrency?", "What local measurements exist?", "Can readers coexist with a writer?"]


def run_local(root, *, native=False, all_gap=False, resume=False, boundary=""):
    def factory(provider):
        original = provider.send

        def send(prompt):
            original_row = json.loads(original(prompt))
            calls = [json.loads(line) for line in (root / "calls.jsonl").read_text().splitlines()]
            turn = next(row["turn"] for row in reversed(calls) if "turn" in row)
            if turn == 1:
                row = {"tool": "research_plan", "args": {"questions": QUESTIONS,
                    "question_details": [{"task_refs": [f"r{n}"]} for n in range(1, 4)]}}
            elif all_gap:
                row = {"tool": "submit_research_answer", "args": {"state": "unresolved", "findings": []}}
            elif turn <= 4:
                row = original_row
            elif turn == 5:
                row = {"tool": "submit_research_answer", "args": {"state": "answered", "findings": ["f1"]}}
            elif turn == 6:
                row = {"tool": "submit_research_answer", "args": {"state": "unresolved", "findings": []}}
            elif turn == 7:
                text = "Readers and a writer may run concurrently. Only one writer at a time."
                ref = passage_ref(URL, hashlib.sha256(text.encode()).hexdigest()[:16], 0, len(text))
                row = {"tool": "save_finding", "args": {"question_ref": "q3", "passage_ref": ref,
                    "claim": "Readers and a writer may run concurrently."}}
            elif turn == 8:
                row = {"tool": "submit_research_answer", "args": {"state": "answered", "findings": ["f2"]}}
            else:
                pytest.fail(f"unexpected extra generation: {turn}: {prompt[-1200:]}")
            return json.dumps(row)

        provider.send = send
        return provider

    return _run(root, native=native, resume=resume, boundary=boundary, task=TASK, budget=12, provider_factory=factory,
                boundary_turn=8 if boundary in {"kernel_settled", "control_ready"} else 5)


@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("native", [False, True])
def test_answer_gap_answer_publishes_one_exact_compilation_and_replays(tmp_path, native):
    result = run_local(tmp_path, native=native)
    assert result.stop_reason == "done", result.summary
    assert result.turns == 8
    report = result.accepted_report
    assert [row["state"] for row in report.coverage] == ["evidence_found", "not_investigated", "evidence_found"]
    assert report.coverage[1]["findings"] == []
    assert report.coverage[2]["answer"] == "Readers and a writer may run concurrently. [f2]"
    assert len(report.selection_receipts) == 3
    calls = (tmp_path / "calls.jsonl").read_bytes()
    assert run_local(tmp_path, native=native, resume=True) == result
    assert (tmp_path / "calls.jsonl").read_bytes() == calls


@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("native", [False, True])
def test_all_gap_terminates_without_finding_or_publication(tmp_path, native):
    result = run_local(tmp_path, native=native, all_gap=True)
    assert result.stop_reason == "research_unresolved", result.summary
    assert not result.synthesis_id and result.accepted_report is None
    assert all(question in result.summary for question in QUESTIONS)
    assert '"publication"' not in (tmp_path / "calls.jsonl").read_text()


@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("boundary", ["provider_response_ready", "kernel_settled", "tool_delivery_pending"])
def test_process_restart_after_local_submit_does_not_repeat_or_advance_twice(tmp_path, boundary):
    source = ("from pathlib import Path; from tests.test_research_local_scope import run_local; import sys; "
              "run_local(Path(sys.argv[1]), boundary=sys.argv[2])")
    child = subprocess.run([sys.executable, "-c", source, str(tmp_path), boundary], capture_output=True, timeout=50)
    assert child.returncode == 74, child.stderr.decode(errors="replace")
    result = run_local(tmp_path, resume=True)
    assert result.stop_reason == "done", result.summary
    assert len(set(result.accepted_report.selection_receipts)) == 3
    calls = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert sum("attempt" in row for row in calls) == 8
    assert sum("fetch" in row for row in calls) == sum("publication" in row for row in calls) == 1


def local_setup(tmp_path):
    from codey.operations.kernel_facts import project_result_observation
    from codey.runtime.core.models import ToolCall
    from tests.test_research_findings import setup

    store, tools, session, delegate = setup(tmp_path)
    result, ok, _ = delegate._execute_research(ToolCall(
        "research_plan", {"questions": ["What limits writer concurrency?"], "question_details": [{"task_refs": ["r1"]}]}))
    assert ok, result.model_text
    project_result_observation(session, result.call, result, ok=True, research_ledger=tools.ledger)
    session._memory_results["plan"] = result
    return store, tools, session, delegate


@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("native", [False, True])
def test_report_length_reopens_only_original_selection_and_accepts_replacement(tmp_path, native):
    long_claim = "同一时刻只有一个写者。" * 6
    short_claim = "WAL模式下，同一时刻只有一个写者，写者串行。"

    def factory(provider):
        original = provider.send

        def send(prompt):
            row = json.loads(original(prompt))
            events = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
            turn = next(event["turn"] for event in reversed(events) if "turn" in event)
            if turn == 1:
                row["args"] = {"questions": [QUESTIONS[0]], "question_details": [{"task_refs": ["r1"]}]}
            elif turn == 4:
                row["args"]["claim"] = long_claim
            elif turn in (5, 7):
                row = {"tool": "submit_research_answer", "args": {"state": "answered", "findings": ["f1" if turn == 5 else "f2"]}}
            elif turn == 6:
                assert "report_length" in prompt or "Report format" in prompt
                text = "Readers and a writer may run concurrently. Only one writer at a time."
                row = {"tool": "save_finding", "args": {"question_ref": "q1", "claim": short_claim,
                    "passage_ref": passage_ref(URL, hashlib.sha256(text.encode()).hexdigest()[:16], 0, len(text))}}
            elif turn > 7:
                pytest.fail(prompt[-2000:])
            return json.dumps(row)

        provider.send = send
        return provider

    result = _run(tmp_path, native=native, task="Explain writer limit. 给出约20字的中文研究结论。", provider_factory=factory)
    assert result.stop_reason == "done", result.summary
    assert result.turns == 7 and long_claim not in result.summary and short_claim in result.summary
    assert result.accepted_report.coverage[0]["findings"] == ["f2"]
    assert len(result.accepted_report.selection_receipts) == 1


@pytest.mark.usefixtures("public_source_dns")
def test_frozen_question_refs_exact_versions_and_stale_scope_are_enforced(tmp_path):
    from dataclasses import replace

    from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn
    from codey.operations.provider_receipts import restore_snapshot, snapshot_description
    from codey.policies.task_policy import TaskPolicy
    from codey.protocols.text_turn import decode_text_turn
    from codey.research.local_scope import submission
    from codey.runtime.core.models import ToolCall, ToolResult
    from tests.test_research_findings import execute

    store, tools, session, delegate = local_setup(tmp_path)
    session.policy = TaskPolicy(session.policy.grants, strict_research=True)
    try:
        opened = execute(delegate, tools, session, "open_url", {"url": URL})
        session._memory_results["read"] = opened
        saved = execute(delegate, tools, session, "save_finding", {
            "claim": "Only one writer can write at a time.", "passage_ref": next(iter(tools.ledger.read_passages))})
        session._memory_results["save"] = saved
        scope = current_scope(session)
        snapshot = build_turn_snapshot(session)
        assert snapshot.research_scope == scope == restore_snapshot(snapshot_description(snapshot)).research_scope
        assert "done" not in snapshot.tool_names and "submit_research_answer" in snapshot.tool_names
        for tool, args in [("open_url", {"url": URL, "question_ref": "q2"}),
                           ("save_finding", {"question_ref": "q1", "passage_ref": "p_unknown", "claim": "An unsupported sentence."}),
                           ("submit_research_answer", {"state": "answered", "findings": ["f99"]})]:
            plan = normalize_turn(decode_text_turn(json.dumps({"tool": tool, "args": args})), snapshot=snapshot)
            assert plan.protocol_error
        args = {"state": "answered", "findings": ["f1"]}
        binding = tools.ledger.references["f1"]
        tools.ledger.references["f1"] = replace(binding, revision=2)
        with pytest.raises(ValueError, match="version differs"):
            submission(session, tools.ledger, scope, args)
        tools.ledger.references["f1"] = binding
        answer = submission(session, tools.ledger, scope, args)
        result = ToolResult(ToolCall("submit_research_answer", args), "Selected", ok=True,
                            canonical={"research_answer": answer})
        session._memory_results["submit"] = result
        session._memory_results["duplicate"] = result
        assert len(effective_answers(session, registered_plan(session))) == 1
        with pytest.raises(ValueError, match="stale"):
            submission(session, tools.ledger, scope, args)
        assert ResearchScope.from_payload(scope.to_payload()) == scope
    finally:
        store.close()
