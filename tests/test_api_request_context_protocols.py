"""The common kernel freezes one latest observation in plain and native APIs."""

import json

import pytest

from codey.operations.kernel_contracts import KernelExecutionDeps, KernelRunRequest, KernelTransportDeps
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers import api_transport
from codey.providers.api_provider import ApiProvider
from codey.research.tools import ResearchTools
from tests.support.kernel_runtime import run_task_kernel


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("protocol", ["openai-completions", "openai-responses"])
def test_latest_research_context_is_ephemeral_in_real_kernel_requests(monkeypatch, native, protocol):
    payloads = []

    def generate(_url, payload, *_args, **_kwargs):
        payloads.append(payload)
        name, args = (("web_search", {"query": "compare outcomes"}) if len(payloads) == 1
                      else ("done", {"summary": "Attempted acquisition; outcome evidence remains missing."}))
        content = json.dumps({"tool": name, "args": args})
        if protocol == "openai-completions":
            message = ({"tool_calls": [{"id": f"c{len(payloads)}", "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)}}]} if native
                       else {"content": content})
            return {"choices": [{"message": message, "finish_reason": "stop"}]}
        output = ([{"type": "function_call", "call_id": f"c{len(payloads)}", "name": name,
                    "arguments": json.dumps(args)}] if native else
                  [{"type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": content}]}])
        return {"output": output, "status": "completed"}

    monkeypatch.setattr(api_transport, "generate", generate)
    provider = ApiProvider("http://fixture/v1", "fixture", native_tools=native, api_protocol=protocol,
                           thinking_enabled=False, text_tool_format="plain", stream=False)
    session = TaskSession(TaskPolicy(frozenset({"control", "web.read"})), task_kind="evidence_followup",
                          task_text="比较结果，中文补查", max_turns=3, research_acquisition_limits=(1, 1))
    class Search:
        def search(self, query, limit=8):
            return []

    tools = ResearchTools(search=Search(), store=None, changes=None)
    try:
        result = run_task_kernel(session, request=KernelRunRequest(
            transport=KernelTransportDeps(provider=provider, provider_id="local", user_task=session.task_text),
            execution=KernelExecutionDeps(research_tools=tools)))
        assert result.completed, result
        assert len(payloads) == 2
        for payload in payloads:
            assert str(payload).count("Research observations (current receipt") == 1
            assert "response_format" not in payload
            assert "_codey_text_results" not in str(payload)
        assert "Research observations" not in str(provider._messages)
        if not native:
            assert "Tool contract (use exactly these shapes)" not in str(provider._messages)
        else:
            assert "tool_calls" in str(payloads[1]) or "function_call" in str(payloads[1])
    finally:
        provider.close()
