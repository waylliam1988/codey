"""Public behavior locks for replacing the implicit task loop."""
from __future__ import annotations

import pytest

from tests.support.operation_trace_probe import BAD, DONE, LENGTH, QUESTION, READ, SHELL, observe_trace


@pytest.mark.parametrize("native", [False, True])
def test_read_then_done_preserves_request_order_and_exact_tool_count(tmp_path, native):
    report = observe_trace(tmp_path, [READ, DONE], native=native)
    assert report["stop_reason"] == "done" and report["completed"]
    assert report["turns"] == 2
    assert report["tool_executions"] == report["tool_receipts"] == 1
    requests = [row for row in report["trace"] if row["event"] in {"send", "send_turn", "send_tool_results"}]
    assert [row["reply"] for row in requests] == ["read_file", "done"]
    assert [row["call_ids"] for row in requests] == ([[], ["call-1"]] if native else [[], []])
    assert report["pending_effects"] == []
    assert report["history_kinds"].count("tool_result") == int(native)


@pytest.mark.parametrize("native", [False, True])
def test_protocol_repair_consumes_the_same_logical_turns(tmp_path, native):
    report = observe_trace(tmp_path, [BAD, DONE], native=native)
    assert report["stop_reason"] == "done"
    assert report["turns"] == 2
    assert report["tool_executions"] == 0
    requests = [row for row in report["trace"] if row["event"] in {"send", "send_turn", "send_tool_results"}]
    assert [row["reply"] for row in requests] == ["unknown_tool", "done"]
    if native:
        assert requests[1]["call_ids"] == ["call-1"]
        assert requests[1]["tools"] == requests[0]["tools"]


def test_length_continuation_is_once_and_counts_as_a_new_turn(tmp_path):
    report = observe_trace(tmp_path, [LENGTH, DONE], native=True)
    assert report["stop_reason"] == "done" and report["turns"] == 2
    assert report["tool_executions"] == 0
    repeated = observe_trace(tmp_path, [LENGTH, LENGTH], native=True)
    assert repeated["stop_reason"] == "provider_failure" and repeated["turns"] == 2
    assert repeated["provider_calls"] == 2


@pytest.mark.parametrize("native", [False, True])
def test_cancelled_provider_reply_never_executes_its_call(tmp_path, native):
    report = observe_trace(tmp_path, [READ], native=native, cancel_after_reply=True)
    assert report["stop_reason"] == "stopped"
    assert report["turns"] == 1 and report["tool_executions"] == 0
    receipts = [row for row in report["trace"] if row["event"] == "acknowledge"]
    assert [row["call_ids"] for row in receipts] == ([["call-1"]] if native else [])


def test_budget_delivers_original_result_and_closes_followups_without_execution(tmp_path):
    report = observe_trace(tmp_path, [READ], native=True, max_turns=1, close_steps=2)
    assert report["stop_reason"] == "max_turns" and report["turns"] == 1
    assert report["tool_executions"] == report["tool_receipts"] == 1
    receipts = [row for row in report["trace"] if row["event"] == "acknowledge"]
    assert [row["call_ids"] for row in receipts] == [["call-1"], ["closing-1"], ["closing-0"]]
    assert receipts[0]["errors"] == [False]
    assert all(row["errors"] == [True] for row in receipts[1:])


@pytest.mark.parametrize("boundary", ["response_returned", "tool_started", "before_post_tools", "after_post_tools"])
def test_fault_probe_interrupts_at_the_observed_boundary(tmp_path, boundary):
    report = observe_trace(tmp_path, [READ, DONE], native=True, interrupt_at=boundary)
    assert report["stop_reason"] == "interrupted"
    assert report["tool_executions"] == (0 if boundary == "response_returned" else 1)
    assert report["tool_receipts"] == (1 if boundary in {"before_post_tools", "after_post_tools"} else 0)
    assert report["provider_calls"] == 1


@pytest.mark.parametrize("native", [False, True])
def test_question_has_stable_identity_and_closes_original_call(tmp_path, native):
    first = observe_trace(tmp_path, [QUESTION], native=native)
    second = observe_trace(tmp_path, [QUESTION], native=native)
    assert first["stop_reason"] == "question" and first["turns"] == 1
    assert first["tool_executions"] == 0
    questions = [row for row in first["trace"] if row["event"] == "question"]
    assert len(questions) == 1
    assert questions == [row for row in second["trace"] if row["event"] == "question"]
    receipts = [row for row in first["trace"] if row["event"] == "acknowledge"]
    assert [row["call_ids"] for row in receipts] == ([["call-1"]] if native else [])


@pytest.mark.parametrize("native", [False, True])
def test_shell_suspends_original_call_without_running_it(tmp_path, native):
    report = observe_trace(tmp_path, [SHELL], native=native, shell_approval=True)
    assert report["stop_reason"] == "approval" and report["turns"] == 1
    assert report["tool_executions"] == report["tool_receipts"] == 0
    approvals = [row for row in report["trace"] if row["event"] == "approval"]
    assert approvals == [{"event": "approval", "call_id": "call-1" if native else "",
                          "turn": 1, "tool_index": 0, "command": SHELL.args["command"]}]
    assert report["pending_effects"] == ["tool_call"]


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("check_failure", [False, True])
def test_completion_rejection_and_check_failure_continue_with_original_receipt(tmp_path, monkeypatch, native, check_failure):
    from codey.operations import completion_gate

    evaluate = completion_gate.evaluate
    evaluations = []

    def gate(session, summary, *, context=None):
        evaluations.append(summary)
        if len(evaluations) == 1:
            if check_failure:
                raise RuntimeError("trace completion check unavailable")
            return completion_gate.GateVerdict(False, "trace check not satisfied")
        return evaluate(session, summary, context=context)

    monkeypatch.setattr(completion_gate, "evaluate", gate)
    report = observe_trace(tmp_path, [DONE, DONE], native=native)
    assert report["completed"] and report["stop_reason"] == "done" and report["turns"] == 2
    assert len(evaluations) == 2 and report["tool_executions"] == 0
    requests = [row for row in report["trace"] if row["event"] in {"send", "send_turn", "send_tool_results"}]
    assert [row["call_ids"] for row in requests] == ([[], ["call-1"]] if native else [[], []])


@pytest.mark.parametrize("native", [False, True])
def test_repeated_identical_results_stop_without_an_extra_provider_request(tmp_path, native):
    report = observe_trace(tmp_path, [READ] * 6, native=native, max_turns=6, stagnant_turns=2)
    assert report["stop_reason"] == "no_progress"
    assert report["turns"] == report["tool_executions"] == report["tool_receipts"] == 3
    assert report["provider_calls"] == (4 if native else 3)
    assert report["pending_effects"] == []
