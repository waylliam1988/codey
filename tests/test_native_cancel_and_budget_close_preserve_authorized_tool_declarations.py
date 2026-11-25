"""Auto-only APIs require the original authorized declarations on terminal delivery."""

import threading

import pytest

from codey.operations.kernel_contracts import KernelExecutionDeps, KernelRunRequest, KernelTransportDeps
from codey.operations.kernel_driver import drive_provider_request
from codey.operations.kernel_protocol import build_turn_snapshot
from codey.operations.provider_request import prepare_provider_request
from codey.operations.provider_session import adapt_provider
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers.base import (
    AssistantTurn,
    ProviderToolCall,
    ProviderToolDefinition,
    ProviderToolResult,
    tools_from_specs,
)
from codey.providers.requests import SendTurnRequest
from tests.support.kernel_fixture import run_task_kernel, runtime


@pytest.mark.parametrize("stop", ["during_send", "received_first_turn", "budget", "no_progress", "invalid_protocol"])
def test_terminal_results_keep_original_declarations_without_executing_new_calls(stop: str) -> None:
    flag = threading.Event()
    max_turns = 2 if stop == "no_progress" else 1
    session = TaskSession(policy=TaskPolicy(frozenset({"control", "project.read"})),
                          task_kind="project", project="", max_turns=max_turns, task_text="Inspect a file")
    snapshot = build_turn_snapshot(session, native=True)
    expected = tools_from_specs(snapshot.frozen_specs)
    name = "undeclared_tool" if stop == "invalid_protocol" else "read_file"
    reply = AssistantTurn(tool_calls=(ProviderToolCall("call-1", name, {"path": "missing.txt"}),))
    closed: list[str] = []

    class Provider:
        native_tools = True

        def send_turn(self, prompt: str, tools: list[ProviderToolDefinition]) -> AssistantTurn:
            assert tools == expected
            if stop == "during_send":
                flag.set()
            return reply

        def send_tool_results(self, results: list[ProviderToolResult], tools: list[ProviderToolDefinition]) -> AssistantTurn:
            pytest.fail("terminal closure must not invite an execution turn")

        def acknowledge_tool_results(self, results: list[ProviderToolResult], declared_tools: list[ProviderToolDefinition],
                                     timeout: float | None = None) -> AssistantTurn:
            assert declared_tools == expected
            closed.extend(result.call_id for result in results)
            return AssistantTurn()

    provider = adapt_provider(Provider())
    with runtime(task="Inspect a file", provider_id="local", max_turns=max_turns) as sink:
        if stop == "received_first_turn":
            sink.bind_request(snapshot, logical_turn=1)
            reference = prepare_provider_request(sink, SendTurnRequest("Inspect a file", tuple(expected)), counters=None)
            drive_provider_request(sink, provider, reference)
            flag.set()
        result = run_task_kernel(session, request=KernelRunRequest(
            transport=KernelTransportDeps(provider=provider, run_id=sink.run_id, provider_id="local", stop_flag=flag,
                                          stagnant_turns=1 if stop in {"no_progress", "invalid_protocol"} else None),
            execution=KernelExecutionDeps(intent_sink=sink)))
    expected_stop = {"budget": "max_turns", "no_progress": "no_progress", "invalid_protocol": "protocol"}.get(stop, "stopped")
    assert result.stop_reason == expected_stop
    assert closed == ["call-1"]
