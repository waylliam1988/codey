"""Control decisions share immutable artifacts and survive interrupted delivery."""
from dataclasses import replace

import pytest

from codey.durable.checkpoint import operation_from_checkpoint
from codey.operations.kernel_control import validate_decision
from codey.operations.task_loop import run_task_kernel
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers.base import AssistantTurn, ProviderToolCall
from tests.durable.test_operation_control_actions import request_for
from tests.durable.test_provider_response_receipts import runtime
from tests.support.kernel_restart import SimulatedExit


@pytest.mark.parametrize("sqlite", [False, True])
def test_repeated_completion_rejection_reuses_the_registered_decision(tmp_path, sqlite):
    durable, _, _, sink = runtime(tmp_path, sqlite=sqlite)
    calls = []

    class Provider:
        def send(self, prompt):
            calls.append(prompt)
            return '{"tool":"done","args":{"summary":"not changed"}}'

    try:
        session = TaskSession(TaskPolicy(frozenset({"control", "project.read"}),
                                        required_checks=("project_changes_required",)), max_turns=3)
        result = run_task_kernel(session, request=request_for(durable, sink, Provider()))
        assert result.stop_reason == "max_turns", result
        assert len(calls) == 3
        decisions = [a for a in durable.artifacts_for_task("r") if a.metadata.get("kind") == "kernel_decision"]
        assert len(decisions) == 2  # One shared rejection and the final closing result.
    finally:
        durable.close()


@pytest.mark.parametrize("payload", [
    {}, {"purpose": "other"}, {"purpose": "done", "result": {}},
    {"purpose": "retry", "prompt": "retry", "error": "", "counters": {}},
    {"purpose": "question", "question_id": "q", "question": {}, "result": {}},
    {"purpose": "closing", "result": None},
])
def test_malformed_decisions_are_rejected_before_any_action(payload):
    with pytest.raises((TypeError, ValueError)):
        validate_decision(payload)


def test_workspace_change_after_saved_completion_requires_a_new_verdict(tmp_path, monkeypatch):
    from codey.operations import completion_gate
    from codey.workspace.revision import WorkspaceRevisionStore

    durable, _, _, sink = runtime(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    target = project / "a.py"
    target.write_text("before\n", encoding="utf-8")
    store = WorkspaceRevisionStore(tmp_path)
    policy = TaskPolicy(frozenset({"control", "project.read"}))
    calls, checks = [], []
    evaluate = completion_gate.evaluate

    def gate(*args, **kwargs):
        checks.append(1)
        return evaluate(*args, **kwargs)

    monkeypatch.setattr(completion_gate, "evaluate", gate)

    class Provider:
        native_tools = True

        def send_turn(self, prompt, tools):
            calls.append("send")
            return AssistantTurn(tool_calls=(ProviderToolCall("done", "done", {"summary": "finished"}),))

        def send_tool_results(self, *args):
            raise AssertionError("no active continuation is needed")

        def acknowledge_tool_results(self, results, tools):
            calls.append("ack")
            return AssistantTurn()

    armed = [True]

    def committed(*_args):
        if armed[0] and operation_from_checkpoint(durable.task("r"), "s").leaf == "control_ready":
            armed[0] = False
            raise SimulatedExit()

    durable.subscribe_commits(committed)
    request = request_for(durable, sink, Provider())
    request = replace(request, execution=replace(request.execution, project_path=project, workspace_revision_store=store))

    def session():
        result = TaskSession(policy, project=str(project), max_turns=4)
        current = store.current_state(project)
        result.set_workspace_state(current.revision, current.fingerprint)
        return result

    try:
        with pytest.raises(SimulatedExit):
            run_task_kernel(session(), request=request)
        assert checks == [1] and calls == ["send"]
        target.write_text("after\n", encoding="utf-8")
        result = run_task_kernel(session(), request=request)
        assert result.completed, result
        assert checks == [1, 1]
        assert calls == ["send", "ack"]
    finally:
        durable.close()
