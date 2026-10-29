"""Local live gates must use the production local response decoder."""

import json
from types import SimpleNamespace

import pytest

from codey.operations.provider_session import normalize_provider_reply
from codey.policies.task_policy import TaskPolicy
from codey.providers import api_transport
from codey.providers.base import AssistantTurn, ProviderToolDefinition
from tests.support.kernel_protocol_test_helpers import normalize_turn
from tests.test_api_generation_observations_no_replay import Response
from tools.local_model_gate_attempts import GateTarget, make_provider

# Original recovery failure, before the edit receipt crash point was reached.
EDIT_FRAME = r'<|tool_call>call:edit{args:{content:"VALUE = 42\n",path:"checkpoint.py"}}<tool_call|>'


@pytest.mark.parametrize("protocol", ["json", "native"])
def test_local_gate_decodes_original_recovery_frame_without_resending(monkeypatch, tmp_path, protocol):
    wire = []

    def reply(request, timeout=None):
        wire.append(json.loads(request.data))
        return Response(json.dumps({"choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": EDIT_FRAME,
        }}]}).encode())

    monkeypatch.setattr(api_transport, "open_request", reply)
    target = GateTarget("http://fixture/v1", "gemma", 32768, 8192, 12000, protocol=protocol)
    provider = make_provider(target, tmp_path)
    try:
        tools = [ProviderToolDefinition("edit", "Write a file", {"type": "object"})]
        raw = provider.send("create checkpoint.py") if protocol == "json" else provider.send_turn("create checkpoint.py", tools)
        turn = normalize_provider_reply(provider, raw)
        assert isinstance(turn, AssistantTurn)
        assert len(turn.tool_calls) == 1
        call = turn.tool_calls[0]
        assert call.name == "edit"
        assert call.arguments == {"path": "checkpoint.py", "content": "VALUE = 42\n"}
        assert call.id == "local-d8ffb06f1336d4fb"
        plan = normalize_turn(turn, policy=TaskPolicy(frozenset({"project.write"})))
        assert not plan.protocol_error
        assert len(plan.calls) == 1
        assert plan.calls[0].call_id == call.id
        assert len(wire) == 1
        assert ("tools" in wire[0]) == (protocol == "native")
        records = [json.loads(line) for line in (tmp_path / "provider.jsonl").read_text(encoding="utf-8").splitlines()]
        assert next(row for row in records if row["type"] == "response")["payload"]["choices"][0]["message"]["content"] == EDIT_FRAME
    finally:
        provider.close()


def test_rejected_fenced_action_is_resent_as_bare_json_before_execution(monkeypatch):
    from codey.operations.task_session import TaskSession
    from codey.runtime.core.models import ToolResult
    from tests.support.kernel_runtime import KernelExecutionDeps, KernelRunRequest, KernelTransportDeps, run_task_kernel

    calls, prompts = [], []
    command = '{"tool":"run","args":{"command":"python -m unittest discover","path":"."}}'
    replies = iter(["```json\n" + command + "\n```", command, '{"tool":"done","args":{"summary":"verified"}}'])

    class Provider:
        native_tools = False

        def send(self, prompt):
            prompts.append(prompt)
            if len(prompts) == 2:
                assert not calls, "a fenced example must never execute"
                assert "one bare JSON tool call" in prompt
                assert "Fenced examples are answer text" in prompt
                assert 'run(args: { "command": string;' in prompt
                assert prompt.count("For actions, reply with exactly one bare JSON tool call") == 1
            return next(replies)

    def execute(call):
        calls.append(call)
        return ToolResult(call, "Ran 1 test\nOK", ok=True, audit={"exit_code": 0})

    monkeypatch.setattr("codey.operations.completion_gate.evaluate", lambda *a, **kw: SimpleNamespace(
        complete=bool(calls), followup="Not done yet (verification_not_locally_observed).", proof=None, final_text=""))
    session = TaskSession(TaskPolicy(frozenset({"control", "project.verify"})), max_turns=4)
    result = run_task_kernel(session, request=KernelRunRequest(
        transport=KernelTransportDeps(provider=Provider(), user_task="Run verification, then finish."),
        execution=KernelExecutionDeps(executors={"run": execute})))
    assert result.completed, result
    assert len(calls) == 1
    assert len(prompts) == 3


def test_reasoned_json_action_executes_through_shared_kernel_before_done(monkeypatch):
    from codey.operations.task_session import TaskSession
    from codey.providers.api_provider import ApiProvider
    from codey.providers.local_response_codec import normalize_local_reply
    from tests.support.kernel_runtime import KernelExecutionDeps, KernelRunRequest, KernelTransportDeps, run_task_kernel
    from tests.support.observed_tool_result import observed_tool_result

    calls, sent = [], []
    replies = iter([
        '{"tool":"web_search","args":{"query":"original study"}}',
        '{"tool":"done","args":{"summary":"Search was executed."}}',
    ])

    def reply(request, timeout=None):
        sent.append(json.loads(request.data))
        return Response(json.dumps({"choices": [{"finish_reason": "stop", "message": {
            "content": next(replies), "reasoning_content": "Gather evidence before answering.",
        }}]}).encode())

    def execute(call):
        calls.append(call)
        return observed_tool_result(call, "Observed search result")

    monkeypatch.setattr(api_transport, "open_request", reply)
    monkeypatch.setattr("codey.operations.completion_gate.evaluate", lambda *a, **kw: SimpleNamespace(
        complete=bool(calls), followup="Search has not executed.", proof=None, final_text=""))
    provider = ApiProvider("http://fixture/v1", "local", native_tools=False, text_decoder=normalize_local_reply)
    try:
        outcome = run_task_kernel(TaskSession(TaskPolicy(frozenset({"control", "web.read"})), max_turns=3),
            request=KernelRunRequest(transport=KernelTransportDeps(provider=provider, user_task="Research a question."),
                                     execution=KernelExecutionDeps(executors={"web_search": execute})))
        assert outcome.completed, outcome
        assert len(calls) == 1 and calls[0].args == {"query": "original study"}
        assert len(sent) == 2
    finally:
        provider.close()
