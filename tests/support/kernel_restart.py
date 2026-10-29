"""Real receipt boundary fixture for restart and delivery failure tests."""
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

from codey.durable.checkpoint import operation_from_checkpoint
from codey.operations.task_loop import run_task_kernel
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers.base import AssistantTurn, ProviderToolCall
from codey.runtime.core.models import ToolResult
from tests.durable.test_operation_control_actions import request_for
from tests.durable.test_provider_response_receipts import runtime


class SimulatedExit(BaseException):
    pass


@contextmanager
def paused_batch(root, *, native=False, pause=True, pause_at="receipt"):
    durable, _, _, sink = runtime(root, task_kind="project")
    events, executions = [], []
    policy = TaskPolicy(frozenset({"control", "project.read"}))

    class Provider:
        native_tools = native

        def send(self, prompt):
            events.append(("send", prompt))
            return ('{"tool":"read_file","args":{"path":"a.py"}}' if len(events) == 1
                    else '{"tool":"done","args":{"summary":"finished"}}')

        def send_turn(self, prompt, tools):
            events.append(("send", prompt))
            return AssistantTurn(tool_calls=(ProviderToolCall("original-read", "read_file", {"path":"a.py"}),))

        def send_tool_results(self, results, tools):
            events.append(("results", [(r.call_id, r.content) for r in results]))
            return AssistantTurn(tool_calls=(ProviderToolCall("original-done", "done", {"summary":"finished"}),))

        def acknowledge_tool_results(self, results, tools):
            events.append(("ack", [(r.call_id, r.content) for r in results]))
            return AssistantTurn()

    def execute(call):
        executions.append(call)
        return ToolResult(call, "ORIGINAL_RESULT", ok=True)

    armed = [pause]
    def committed(*_args):
        state = operation_from_checkpoint(durable.task("r"), "s")
        if armed[0] and state.leaf == {"receipt": "tool_delivery_pending", "intent": "tool_effect_pending",
                                      "provider_intent": "provider_effect_pending"}[pause_at]:
            armed[0] = False
            raise SimulatedExit()

    durable.subscribe_commits(committed)
    request = request_for(durable, sink, Provider(), executors={"read_file": execute})
    request = replace(request, transport=replace(request.transport,
        user_task="ORIGINAL_GOAL: inspect a.py", context_text="ALLOWED_CONTRACT: read_file, done"),
        observation=replace(request.observation, propagate_provider_failure=False))
    fixture = SimpleNamespace(durable=durable, sink=sink, request=request, events=events, executions=executions,
                              session=lambda: TaskSession(policy, task_kind="project", max_turns=4,
                                                          task_text="ORIGINAL_GOAL: inspect a.py"))
    try:
        if pause:
            try:
                run_task_kernel(fixture.session(), request=request)
            except SimulatedExit:
                pass
            else:
                raise AssertionError("fixture did not reach the committed receipt boundary")
        yield fixture
    finally:
        durable.close()


@contextmanager
def approved_shell(root, receipt, *, call_id="original-shell", command_change=None):
    """Pause the real driver, publish an approval receipt, then reopen SQLite."""
    from codey.durable import ApprovalRecord, ApprovalStatus, DurableSession, EffectRecord, SQLiteDurableStore
    from codey.operations.generation_boundary import durable_generation_boundary_callback
    from codey.operations.task_effects import KernelEffectSink
    from codey.providers.response_record import json_value
    from codey.runtime.write.mutation_line import RuntimeMutationLine

    durable, mutations, outputs, sink = runtime(root, sqlite=True)
    original = {"command": "echo hi", "path": "."}
    events = []

    class Provider:
        native_tools = True

        def send_turn(self, prompt, tools):
            events.append(("send", prompt))
            return AssistantTurn(tool_calls=(ProviderToolCall(call_id, "shell", original),))

        def send_tool_results(self, results, tools):
            events.append(("results", list(results)))
            return AssistantTurn(tool_calls=(ProviderToolCall("done", "done", {"summary": "reported"}),))

        def acknowledge_tool_results(self, results, tools):
            events.append(("ack", list(results)))
            return AssistantTurn()

    def publish(request):
        command = {**json_value(request), "tool_name": request.tool_name, "tool_args": original}
        if command_change is not None:
            command_change(command)
        ref = outputs.write_json_artifact(session_id="s", run_id="r", payload=command)
        durable.create_approval(ApprovalRecord("approval", "r", "s", ApprovalStatus.REQUESTED,
                                               ref.sha256, ref.handle, "."))

    def forbidden(_call):
        raise AssertionError("approval reconciliation must not execute shell")

    policy = TaskPolicy(frozenset({"control", "project.read", "project.verify", "shell.approval"}))
    def session():
        return TaskSession(policy, max_turns=4)
    request = request_for(durable, sink, Provider(), executors={"shell": forbidden})
    request = replace(request, execution=replace(request.execution, project_path=root),
                      observation=replace(request.observation, on_shell_request=publish,
                                          propagate_provider_failure=False))
    try:
        first = run_task_kernel(session(), request=request)
        if command_change is None:
            assert first.stop_reason == "approval", first
            durable.wait_task("r", state={"stop_reason": "approval"})
            record = durable.approval("approval")
            denied = receipt.get("status") == "denied"
            status = ApprovalStatus.DENIED if denied else ApprovalStatus.APPROVED
            durable.transition_approval("approval", replace(record, status=status, revision=record.revision + 1),
                                        expected_status=ApprovalStatus.REQUESTED)
            if not denied:
                durable.claim_approval_and_begin_effect("approval", EffectRecord(
                    effect_id="shell-approval", task_id="r", category="shell", replay_class="unsafe",
                    args_digest=record.command_digest, status="pending", intent={"approval_id": "approval"}))
            ref = outputs.write_json_artifact(session_id="s", run_id="r", payload=receipt)
            durable.settle_approval("approval", settlement={"result_ref": ref.handle, "result_digest": ref.sha256},
                                    resume=True)
        durable.close()
        durable = DurableSession(SQLiteDurableStore(root / "runtime.sqlite"))
        if durable.task("r").status == "pending":
            durable.claim_task("r")
        mutations = RuntimeMutationLine(durable=durable)
        sink = KernelEffectSink(mutations, session_id="s", run_id="r", provider_id="local", durable=durable,
                                managed_outputs=outputs)
        request = replace(request, observation=replace(request.observation, intent_sink=sink, on_shell_request=None,
            on_boundary=durable_generation_boundary_callback(durable=durable, session_id="s", task_id="r", sink=sink)))
        yield SimpleNamespace(durable=durable, sink=sink, outputs=outputs, request=request,
                              events=events, session=session, initial=first)
    finally:
        durable.close()
