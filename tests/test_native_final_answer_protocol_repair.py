"""Malformed native answer objects are repaired, never reinterpreted as success."""
import copy

from codey.operations.task_loop import KernelRunRequest, KernelTransportDeps, run_task_kernel
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers import api_transport
from codey.providers.api_provider import ApiProvider


def test_native_answer_object_must_be_resent_through_declared_done(monkeypatch):
    requests = []
    answer = '{"product":323}'

    def generate(url, payload, *args, **kwargs):
        requests.append(copy.deepcopy(payload))
        if len(requests) == 1:
            calls = [{"product": 323, "id": "malformed-answer"}]
        else:
            assert len(requests) == 2
            prompt = payload["messages"][-1]["content"]
            assert "final answer" in prompt and "summary string" in prompt
            assert "requested final format" in prompt
            calls = [{"id": "done-1", "type": "function",
                      "function": {"name": "done", "arguments": '{"summary":"{\\"product\\":323}"}'}}]
        return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": calls},
                             "finish_reason": "tool_calls"}]}

    monkeypatch.setattr(api_transport, "generate", generate)
    monkeypatch.setattr("codey.operations.kernel_transport.provider_uses_native", lambda *a, **kw: True)
    provider = ApiProvider("http://localhost:9/v1", "fixture")
    session = TaskSession(policy=TaskPolicy(frozenset({"control"})), max_turns=3)
    result = run_task_kernel(session, request=KernelRunRequest(transport=KernelTransportDeps(
        provider=provider, run_id="answer-repair", user_task="Return the product as JSON.", provider_id="local")))
    assert result.completed and result.summary == answer
    assert result.turns == 2 and len(requests) == 2
    assert not session.executed
    assert result.proof is not None and result.proof.satisfied
