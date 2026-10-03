"""Repair a malformed reply using the frozen tools and the actual transport."""

from types import SimpleNamespace

import pytest

from codey.operations import kernel_prompt
from codey.operations.task_loop import KernelExecutionDeps, KernelRunRequest, KernelTransportDeps, run_task_kernel
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers.base import AssistantTurn
from codey.runtime.core.models import ToolResult


def test_missing_tool_name_is_not_inferred_and_json_repair_keeps_visible_contract():
    prompts = []
    executions = []

    def send(prompt):
        prompts.append(prompt)
        if len(prompts) == 1:
            return '<|tool_call>call:tool{args:{symbol:"calculate_total",path:"."}}<tool_call|>'
        assert executions == []
        assert '"tool":"find_references"' in prompt.replace(" ", "")
        assert "knowledge_write" not in prompt and '"tool":"edit"' not in prompt.replace(" ", "")
        assert "tags" in prompt.lower()
        return '{"tool":"find_references","args":{"symbol":"calculate_total","path":"."}}'

    def find(call):
        executions.append(call)
        return ToolResult(call=call, model_text="no references")

    result = run_task_kernel(
        TaskSession(policy=TaskPolicy(grants=frozenset({"control", "project.read"})), max_turns=2),
        request=KernelRunRequest(
            transport=KernelTransportDeps(provider=SimpleNamespace(send=send), run_id="repair-snapshot"),
            execution=KernelExecutionDeps(executors={"find_references": find}),
        ),
    )
    assert result.stop_reason == "max_turns"
    assert len(executions) == 1 and executions[0].name == "find_references"


def test_native_repair_requests_native_calls_instead_of_json(monkeypatch):
    prompts = []

    def send_turn(prompt, tools=None):
        prompts.append(prompt)
        if len(prompts) == 2:
            assert "native tool" in prompt.lower()
            assert "exactly one JSON object" not in prompt
        return AssistantTurn(text="ordinary prose")

    monkeypatch.setattr("codey.operations.kernel_transport.provider_uses_native", lambda *args, **kwargs: True)
    result = run_task_kernel(
        TaskSession(policy=TaskPolicy(grants=frozenset({"control"})), max_turns=2),
        request=KernelRunRequest(
            transport=KernelTransportDeps(provider=SimpleNamespace(send_turn=send_turn), run_id="native-repair"),
        ),
    )
    assert len(prompts) == 2
    assert result.stop_reason == "max_turns"


def test_repair_uses_supplied_contract_without_consulting_live_registry(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("repair must not rebuild the captured contract")

    monkeypatch.setattr(kernel_prompt, "_snapshot_names", forbidden)
    contract = 'frozen tools: {"tool":"done","args":{"summary":"complete"}}'
    assert contract in kernel_prompt._repair_prompt("no JSON tool call found", contract_text=contract, native=False)
