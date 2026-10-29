"""The kernel exchanges frozen definitions and results, never API wire messages."""
from dataclasses import fields
from types import SimpleNamespace

from codey.operations import kernel_protocol, kernel_transport
from codey.policies.task_policy import TaskPolicy
from codey.providers import base


def test_turn_snapshot_has_no_chat_completions_tool_wrapper():
    session = SimpleNamespace(policy=TaskPolicy(grants=frozenset({"control"})), controller_denied=())
    snapshot = kernel_protocol.build_turn_snapshot(session, native=True)
    assert snapshot.frozen_specs
    assert "native_tools" not in {f.name for f in fields(snapshot)}


def test_native_result_delivery_contains_no_api_message_keys(monkeypatch):
    monkeypatch.setattr("codey.operations.kernel_prompt._result_context", lambda result, session: "ERROR: denied")
    result = SimpleNamespace(call=SimpleNamespace(call_id="call-exact"))
    delivered = kernel_transport._native_tool_results([result], object())
    assert delivered == [base.ProviderToolResult("call-exact", "ERROR: denied")]


def test_output_limit_is_a_typed_fact_for_every_native_provider():
    from codey.operations.task_session import TaskSession
    from tests.support.kernel_runtime import KernelRunRequest, KernelTransportDeps, run_task_kernel
    calls = []
    class Provider:
        native_tools = True
        def send_turn(self, prompt, tools):
            calls.append(prompt)
            return base.AssistantTurn(finish=base.TurnFinish.OUTPUT_LIMIT)
        def send_tool_results(self, *args):
            raise AssertionError("no tool calls to answer")
    result = run_task_kernel(TaskSession(TaskPolicy(frozenset({"control"})), max_turns=4),
                            request=KernelRunRequest(transport=KernelTransportDeps(provider=Provider(), provider_id="local")))
    assert result.stop_reason == "provider_failure"
    assert len(calls) == 2


def test_raw_diagnostics_do_not_authorize_output_limit_continuation():
    from codey.operations.task_session import TaskSession
    from tests.support.kernel_runtime import KernelRunRequest, KernelTransportDeps, run_task_kernel
    calls = []
    class Provider:
        native_tools = True
        def send_turn(self, prompt, tools):
            calls.append(prompt)
            return base.AssistantTurn(text="diagnostic", raw={"continuable_length": True})
        def send_tool_results(self, *args):
            raise AssertionError("no tool calls to answer")
    result = run_task_kernel(TaskSession(TaskPolicy(frozenset({"control"})), max_turns=4),
                            request=KernelRunRequest(transport=KernelTransportDeps(provider=Provider(), provider_id="local")))
    assert result.completed and result.stop_reason == "done"
    assert len(calls) == 1
