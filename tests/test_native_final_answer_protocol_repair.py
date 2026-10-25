"""Malformed native answer objects are repaired, never reinterpreted as success."""
import copy
import json

import pytest

from codey.operations.task_loop import KernelRunRequest, KernelTransportDeps, run_task_kernel
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers import api_transport
from codey.providers.api_provider import ApiProvider


@pytest.mark.parametrize("mistake", ["malformed_object", "fenced_summary"])
def test_native_answer_object_must_be_resent_through_declared_done(monkeypatch, mistake):
    requests = []
    answer = '{"product":323}'

    def generate(url, payload, *args, **kwargs):
        requests.append(copy.deepcopy(payload))
        if len(requests) == 1:
            assert "summary string" in payload["messages"][0]["content"]
            calls = ([{"product": 323, "id": "malformed-answer"}] if mistake == "malformed_object" else
                     [{"id": "fenced-answer", "type": "function", "function": {"name": "done",
                       "arguments": json.dumps({"summary": "```json\n" + answer + "\n```"})}}])
        else:
            assert len(requests) == 2
            prompt = next(message["content"] for message in reversed(payload["messages"])
                          if not str(message.get("content", "")).startswith("<request_context>"))
            assert "final answer" in prompt and "summary string" in prompt
            assert "requested final format" in prompt
            assert "not the outer tool-call object" in prompt
            if mistake == "fenced_summary":
                assert "without Markdown fences" in prompt
            calls = [{"id": "done-1", "type": "function",
                      "function": {"name": "done", "arguments": '{"summary":"{\\"product\\":323}"}'}}]
        return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": calls},
                             "finish_reason": "tool_calls"}]}

    monkeypatch.setattr(api_transport, "generate", generate)
    monkeypatch.setattr("codey.operations.kernel_transport.provider_uses_native", lambda *a, **kw: True)
    provider = ApiProvider("http://localhost:9/v1", "fixture")
    session = TaskSession(policy=TaskPolicy(frozenset({"control"}), required_checks=("output_json",)), max_turns=3)
    result = run_task_kernel(session, request=KernelRunRequest(transport=KernelTransportDeps(
        provider=provider, run_id="answer-repair", user_task="Return the product as JSON.", provider_id="local")))
    assert result.completed and result.summary == answer
    assert result.turns == 2 and len(requests) == 2
    assert not session.executed
    assert result.proof is not None and result.proof.satisfied
