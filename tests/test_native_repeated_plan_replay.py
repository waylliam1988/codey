"""Replay the reported Qwen wire shape through the production API and kernel."""
import json

import pytest

from codey.operations.kernel_contracts import (
    KernelExecutionDeps,
    KernelObservationDeps,
    KernelRunRequest,
    KernelTransportDeps,
)
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers import api_transport
from codey.providers.api_provider import ApiProvider
from codey.runtime.core.models import ToolResult
from tests.support.kernel_runtime import run_task_kernel
from tests.test_api_chat_native_turns_and_history import _FakeResponse

QUESTIONS = ["网站有哪些 API 端点？", "认证机制是什么？是否有绕过可能？",
             "是否存在常见的 Web 漏洞（如 SQL 注入、XSS、CSRF）？",
             "API 是否有速率限制或枚举漏洞？", "是否存在信息泄露（如版本信息、调试信息）？"]
THOUGHT = ("This is a security research task. Let me plan my approach:\n\n"
           "1. First, I should register this as a research task with specific questions\n"
           "2. Then investigate the website's API endpoints, authentication mechanisms, and common vulnerability patterns\n\n"
           "Let me start by registering the research questions.")


@pytest.mark.parametrize("obeys_feedback", [False, True])
def test_native_wire_replay_preserves_results_thinking_and_bounded_recovery(monkeypatch, obeys_feedback):
    wire, events, executed = [], [], []

    def respond(request, timeout=None):
        payload = json.loads(request.data)
        wire.append(payload)
        corrected = obeys_feedback and any("already succeeded" in str(row.get("content", ""))
                                          for row in payload["messages"] if row.get("role") == "user")
        name = "done" if corrected else "research_plan"
        args = {"summary": "The plan is recorded. Investigation remains incomplete."} if corrected else {"questions": QUESTIONS}
        message = {"content": "\n\n", "reasoning_content": THOUGHT, "tool_calls": [{
            "id": f"call_{len(wire)}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
        }]}
        return _FakeResponse(json.dumps({"choices": [{"finish_reason": "tool_calls", "message": message}]}).encode())

    def execute(call):
        executed.append(call)
        return ToolResult(call, json.dumps({"questions": QUESTIONS, "next": "Investigate the questions."}), ok=True,
                          canonical={"research_observation": {"questions": QUESTIONS}})

    monkeypatch.setattr(api_transport, "open_request", respond)
    provider = ApiProvider("http://fixture.invalid/v1", "qwen-replay", native_tools=True)
    try:
        result = run_task_kernel(TaskSession(TaskPolicy(frozenset({"knowledge.read", "control"})), max_turns=8),
            request=KernelRunRequest(transport=KernelTransportDeps(provider, user_task="Register a plan and explain the gaps."),
                execution=KernelExecutionDeps(executors={"research_plan": execute}),
                observation=KernelObservationDeps(on_event=events.append)))
        assert result.stop_reason == ("done" if obeys_feedback else "no_progress"), result.summary
        assert len(executed) == (2 if obeys_feedback else 3)
        assert len(wire) == 3  # Closure acknowledges pending IDs without generating again.
        for index, request in enumerate(wire):
            receipts = [row for row in request["messages"] if row.get("role") == "tool"]
            assert [row["tool_call_id"] for row in receipts] == [f"call_{n}" for n in range(1, index + 1)]
            assert all("Investigate the questions." in row["content"] for row in receipts)
        assert any("already succeeded" in str(row.get("content", ""))
                   for row in wire[2]["messages"] if row.get("role") == "user")
        assert all(event.reasoning == THOUGHT and event.reply == "\n\n" for event in events if event.kind == "turn")
    finally:
        provider.close()
