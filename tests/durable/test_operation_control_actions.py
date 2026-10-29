"""Control delivery uses committed decisions and the original effect ledger."""
from __future__ import annotations

from dataclasses import replace

import pytest

from codey.operations.generation_boundary import durable_generation_boundary_callback
from codey.operations.task_loop import (
    KernelExecutionDeps,
    KernelObservationDeps,
    KernelRunRequest,
    KernelTransportDeps,
    run_task_kernel,
)
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers.base import AssistantTurn, ProviderToolCall
from tests.durable.test_provider_response_receipts import runtime


def request_for(durable, sink, provider, *, on_event=None, executors=None):
    return KernelRunRequest(transport=KernelTransportDeps(provider=provider, run_id="r", provider_id="local"),
        execution=KernelExecutionDeps(executors=executors),
        observation=KernelObservationDeps(intent_sink=sink, on_event=on_event, propagate_provider_failure=True,
            on_boundary=durable_generation_boundary_callback(durable=durable, session_id="s", task_id="r", sink=sink)))


@pytest.mark.parametrize("sqlite", [False, True])
def test_settled_result_restores_original_completion_proposal(tmp_path, sqlite):
    durable, _, _, sink = runtime(tmp_path, sqlite=sqlite)
    sends = []

    class Provider:
        native_tools = True

        def send_tool_results(self, results, tools):
            raise AssertionError("unexpected continuation")

        def send_turn(self, prompt, tools):
            sends.append("request")
            return AssistantTurn(tool_calls=(ProviderToolCall("done-original", "done", {"summary": "finished"}),))

        def acknowledge_tool_results(self, results, tools):
            sends.append("ack")
            return AssistantTurn()

    try:
        request = request_for(durable, sink, Provider())
        original = TaskSession(TaskPolicy(frozenset({"control"})))
        first = run_task_kernel(original, request=request)
        assert first.completed, first
        resumed = TaskSession(original.policy)
        second = run_task_kernel(resumed, request=request)
        assert second == first
        assert sends == ["request", "ack"]
        assert resumed.last_done_text == original.last_done_text == "finished"
        assert resumed.last_done_args == original.last_done_args == {"summary": "finished"}
    finally:
        durable.close()


@pytest.mark.parametrize("sqlite", [False, True])
def test_durable_cancel_after_response_saves_reply_and_only_admits_close_receipts(tmp_path, sqlite):
    durable, _, _, sink = runtime(tmp_path, sqlite=sqlite)
    executed, calls = [], []

    class Provider:
        native_tools = True

        def send_tool_results(self, *args):
            raise AssertionError("unexpected normal continuation")

        def send_turn(self, prompt, tools):
            calls.append("request")
            durable.request_cancel("r")
            return AssistantTurn(tool_calls=(ProviderToolCall("original", "read_file", {"path": "a.py"}),))

        def acknowledge_tool_results(self, results, tools):
            calls.append([row.call_id for row in results])
            assert all(row.content.startswith("ERROR:") for row in results)
            return AssistantTurn()

    try:
        result = run_task_kernel(TaskSession(TaskPolicy(frozenset({"control", "project.read"})), max_turns=4),
            request=request_for(durable, sink, Provider(), executors={"read_file": executed.append}))
        assert result.stop_reason == "stopped", result
        assert not executed
        assert calls == ["request", ["original"]]
        effects = durable.effects("r")
        assert all(effect.category == "provider_send" and effect.settlement is not None for effect in effects)
        assert durable.task("r").state["cancel_requested"]
    finally:
        durable.close()


def test_question_survives_notification_failure_before_protocol_acknowledgement(tmp_path):
    from codey.durable.questions import DurableUserQuestionStore

    durable, _, _, sink = runtime(tmp_path)
    requests, answers = [], []

    class Provider:
        native_tools = True

        def send_tool_results(self, *args):
            raise AssertionError("unexpected normal continuation")

        def send_turn(self, prompt, tools):
            requests.append(prompt)
            return AssistantTurn(tool_calls=(ProviderToolCall("question-call", "ask_user",
                {"question": "Choose", "options": ["A", "B"]}),))

        def acknowledge_tool_results(self, results, tools):
            answers.append([row.call_id for row in results])
            return AssistantTurn()

    def unavailable(_event):
        raise OSError("notification unavailable")

    try:
        provider = Provider()
        req = request_for(durable, sink, provider, on_event=unavailable)
        # The first turn event is informational; fail the question notification.
        req = replace(req, observation=replace(req.observation, on_event=lambda event:
            unavailable(event) if event.kind == "user_question" else None))
        first = run_task_kernel(TaskSession(TaskPolicy(frozenset({"control"}))), request=req)
        assert first.stop_reason == "delivery_pending", first
        question = DurableUserQuestionStore(durable).pending("s")
        assert question and question["question"] == "Choose"
        assert not answers
        seen = []
        resumed = run_task_kernel(TaskSession(TaskPolicy(frozenset({"control"}))),
            request=replace(req, observation=replace(req.observation, on_event=seen.append)))
        assert resumed.stop_reason == "question", resumed
        assert len(requests) == 1 and answers == [["question-call"]]
        assert DurableUserQuestionStore(durable).pending("s")["id"] == question["id"]
    finally:
        durable.close()


def test_known_not_sent_done_acknowledgement_retries_delivery_without_rechecking_gate(tmp_path, monkeypatch):
    from codey.operations import completion_gate
    from codey.providers.error_classification import RequestPrepError

    durable, _, _, sink = runtime(tmp_path)
    evaluations, sends = [], []
    evaluate = completion_gate.evaluate

    def gate(*args, **kwargs):
        evaluations.append(True)
        return evaluate(*args, **kwargs)

    monkeypatch.setattr(completion_gate, "evaluate", gate)

    class Provider:
        native_tools = True

        def send_tool_results(self, *args):
            raise AssertionError("unexpected normal continuation")

        def send_turn(self, prompt, tools):
            return AssistantTurn(tool_calls=(ProviderToolCall("done-call", "done", {"summary": "finished"}),))

        def acknowledge_tool_results(self, results, tools):
            sends.append([row.call_id for row in results])
            if len(sends) == 1:
                raise RequestPrepError("not submitted")
            return AssistantTurn()

    try:
        req = request_for(durable, sink, Provider())
        first = run_task_kernel(TaskSession(TaskPolicy(frozenset({"control"}))), request=req)
        assert first.stop_reason == "delivery_pending", first
        assert first.proof is not None
        result = run_task_kernel(TaskSession(TaskPolicy(frozenset({"control"}))), request=req)
        assert result.completed, result
        assert evaluations == [True]
        assert sends == [["done-call"], ["done-call"]]
    finally:
        durable.close()


def test_final_boundary_places_steering_and_invalidates_saved_completion_atomically(tmp_path):
    from codey.durable.model import SubmissionPlacement

    durable, _, _, sink = runtime(tmp_path)
    calls = []

    class Provider:
        native_tools = True

        def send_tool_results(self, *args):
            raise AssertionError("unexpected tool continuation")

        def send_turn(self, prompt, tools):
            calls.append(prompt)
            return AssistantTurn(tool_calls=(ProviderToolCall(f"done-{len(calls)}", "done", {"summary": "finished"}),))

        def acknowledge_tool_results(self, results, tools):
            if len(calls) == 1:
                durable.admit_input(conversation_id="s", request_id="steer", task_id="steer", text="also explain why",
                    provider_snapshot={}, placement=SubmissionPlacement.QUEUED, mode="steer")
            return AssistantTurn()

    try:
        result = run_task_kernel(TaskSession(TaskPolicy(frozenset({"control"})), max_turns=4),
            request=request_for(durable, sink, Provider()))
        assert result.completed and result.turns == 2, result
        assert len(calls) == 2 and "also explain why" in calls[1]
        assert durable.task("steer").state["consumed_by_task_id"] == "r"
    finally:
        durable.close()


def test_cancellation_during_completion_check_prevents_success_ack(tmp_path, monkeypatch):
    from codey.operations import completion_gate

    durable, _, _, sink = runtime(tmp_path)
    acknowledgements = []
    evaluate = completion_gate.evaluate

    def gate(*args, **kwargs):
        verdict = evaluate(*args, **kwargs)
        durable.request_cancel("r")
        return verdict

    monkeypatch.setattr(completion_gate, "evaluate", gate)

    class Provider:
        native_tools = True

        def send_tool_results(self, *args):
            raise AssertionError("unexpected continuation")

        def send_turn(self, prompt, tools):
            return AssistantTurn(tool_calls=(ProviderToolCall("done", "done", {"summary": "finished"}),))

        def acknowledge_tool_results(self, results, tools):
            acknowledgements.extend(row.content for row in results)
            return AssistantTurn()

    try:
        result = run_task_kernel(TaskSession(TaskPolicy(frozenset({"control"}))), request=request_for(durable, sink, Provider()))
        assert result.stop_reason == "stopped" and not result.completed, result
        assert len(acknowledgements) == 1 and acknowledgements[0].startswith("ERROR:")
    finally:
        durable.close()


@pytest.mark.parametrize("cancelled", [False, "active", "parked"])
def test_approval_resume_uses_original_call_and_never_executes_shell(tmp_path, cancelled):
    from codey.durable import ApprovalRecord, ApprovalStatus
    from codey.providers.response_record import json_value

    durable, _, outputs, sink = runtime(tmp_path)
    original = {"command": "echo hi", "path": "."}
    sent = []

    def request_approval(request):
        command = outputs.write_json_artifact(session_id="s", run_id="r", payload={
            **json_value(request), "tool_name": request.tool_name, "tool_args": original,
            "turn": request.turn, "tool_index": request.tool_index})
        durable.create_approval(ApprovalRecord("approval", "r", "s", ApprovalStatus.REQUESTED,
            command.sha256, command.handle, "."))

    class Provider:
        native_tools = True

        def send_turn(self, prompt, tools):
            return AssistantTurn(tool_calls=(ProviderToolCall("original-shell", "shell", original),))

        def send_tool_results(self, results, tools):
            sent.extend(results)
            return AssistantTurn(tool_calls=(ProviderToolCall("done", "done", {"summary": "reported"}),))

        def acknowledge_tool_results(self, results, tools):
            sent.extend(results)
            return AssistantTurn()

    def forbidden(call):
        raise AssertionError("the command must be owned by the approval workflow")

    session = TaskSession(TaskPolicy(frozenset({"control", "project.read", "project.verify", "shell.approval"})), max_turns=4)
    req = request_for(durable, sink, Provider(), executors={"shell": forbidden})
    req = replace(req, execution=replace(req.execution, project_path=tmp_path),
        observation=replace(req.observation, on_shell_request=request_approval))
    try:
        first = run_task_kernel(session, request=req)
        assert first.stop_reason == "approval", first
        if cancelled:
            if cancelled == "parked":
                durable.wait_task("r", state={"stop_reason": "approval"})
            durable.request_cancel("r")
            if cancelled == "parked":
                durable.claim_task("r")
            stopped = run_task_kernel(session, request=req)
            assert stopped.stop_reason == "stopped", stopped
            assert sent[0].call_id == "original-shell"
            assert all(effect.settlement is not None for effect in durable.effects("r"))
            assert not any(effect.category == "shell" for effect in durable.effects("r"))
            return
        durable.wait_task("r", state={"stop_reason": "approval"})
        approval = durable.approval("approval")
        durable.transition_approval("approval", replace(approval, status=ApprovalStatus.DENIED, revision=1),
                                    expected_status=ApprovalStatus.REQUESTED)
        result = outputs.write_json_artifact(session_id="s", run_id="r", payload={"ok": False,
            "status": "denied", "output": "Denied by user", "exit_code": None})
        durable.settle_approval("approval", settlement={"result_ref": result.handle, "result_digest": result.sha256}, resume=True)
        durable.claim_task("r")
        resumed = run_task_kernel(session, request=req)
        assert resumed.completed, resumed
        assert sent[0].call_id == "original-shell" and sent[0].content
        effects = [effect for effect in durable.effects("r") if effect.category == "tool_call"]
        assert len(effects) == 1 and effects[0].settlement is not None
        assert not any(effect.category == "shell" for effect in durable.effects("r"))
    finally:
        durable.close()
