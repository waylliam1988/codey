"""Auto-only APIs require the original authorized declarations on terminal delivery."""

import threading

import pytest

from codey.operations.kernel_protocol import build_turn_snapshot
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers.base import AssistantTurn, ProviderToolCall, tools_from_specs
from tests.support.kernel_runtime import KernelObservationDeps, KernelRunRequest, KernelTransportDeps, run_task_kernel


@pytest.mark.parametrize("stop", ["during_send", "received_first_turn", "budget", "no_progress", "invalid_protocol"])
def test_terminal_results_keep_original_declarations_without_executing_new_calls(stop, tmp_path):
    flag = threading.Event()
    session = TaskSession(policy=TaskPolicy(frozenset({"control", "project.read"})),
                          task_kind="project", project="", max_turns=1, task_text="Inspect a file")
    snapshot = build_turn_snapshot(session, native=True)
    expected = tools_from_specs(snapshot.frozen_specs)
    name = "undeclared_tool" if stop == "invalid_protocol" else "read_file"
    reply = AssistantTurn(tool_calls=(ProviderToolCall("call-1", name, {"path": "missing.txt"}),))
    closed = []

    class Provider:
        native_tools = True

        def send_turn(self, prompt, tools):
            assert tools == expected
            if stop == "during_send":
                flag.set()
            return reply

        def send_tool_results(self, results, tools):
            pytest.fail("terminal closure must not invite an execution turn")

        def acknowledge_tool_results(self, results, declared_tools, timeout=None):
            assert declared_tools == expected
            closed.extend(result.call_id for result in results)
            return AssistantTurn()

    observation = KernelObservationDeps()
    if stop == "received_first_turn":
        from codey.operations.generation_boundary import durable_generation_boundary_callback
        from tests.durable.test_provider_response_receipts import runtime
        from tests.support.provider_actions import ProviderActions
        durable, _, _, sink = runtime(tmp_path)
        sink.bind_request(snapshot, logical_turn=1)
        ProviderActions(Provider(), sink).invoke("send_turn", "Inspect a file", expected)
        flag.set()
        observation = KernelObservationDeps(intent_sink=sink, on_boundary=durable_generation_boundary_callback(
            durable=durable, session_id="s", task_id="r", sink=sink))
    result = run_task_kernel(session, request=KernelRunRequest(
        transport=KernelTransportDeps(provider=Provider(), stop_flag=flag,
                                      stagnant_turns=1 if stop in {"no_progress", "invalid_protocol"} else None),
        observation=observation))
    expected_stop = {"budget": "max_turns", "no_progress": "no_progress", "invalid_protocol": "protocol"}.get(stop, "stopped")
    assert result.stop_reason == expected_stop
    assert closed == ["call-1"]
