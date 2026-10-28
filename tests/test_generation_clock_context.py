"""Actual generation envelopes carry one clock observation, history stays exact."""
import copy
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from codey.providers import api_transport, request_context
from codey.providers.api_provider import ApiProvider
from codey.providers.base import ProviderToolDefinition, ProviderToolResult
from codey.providers.token_accounting import RequestContextCount


def test_clock_reports_host_timezone_source_and_correct_utc(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls):
            return cls(2026, 10, 24, 0, 30, tzinfo=timezone(timedelta(hours=8), "Host +08"))

        def astimezone(self, tz=None):
            return self if tz is None else super().astimezone(tz)

    monkeypatch.setattr(request_context, "datetime", Clock)
    text = request_context.clock_context()
    data = json.loads(text.splitlines()[1])
    assert data["local_date"] == "2026-10-24"
    assert data["observed_at_utc"] == "2026-10-23T16:30:00+00:00"
    assert data["utc_offset"] == "+0800" and data["timezone_name"] == "Host +08"
    assert data["timezone_source"] == "execution_host_operating_system"


@pytest.mark.parametrize("protocol", ["openai-completions", "openai-responses"])
def test_initial_and_tool_continuation_count_and_send_same_clock_without_rewriting_history(monkeypatch, protocol):
    clock = iter(["<request_context>clock-1</request_context>", "<request_context>clock-2</request_context>"])
    monkeypatch.setattr(request_context, "clock_context", lambda: next(clock))
    counted, sent = [], []

    def count(payload, **kwargs):
        counted.append(copy.deepcopy(payload))
        return RequestContextCount(100, "estimated")

    def generate(url, payload, headers, **kwargs):
        sent.append(copy.deepcopy(payload))
        if protocol == "openai-completions":
            message = {"role": "assistant", "content": "ok"}
            if len(sent) == 1:
                message["tool_calls"] = [{"id": "c1", "type": "function", "function": {
                    "name": "read_file", "arguments": '{"path":"a.py"}'}}]
            return {"choices": [{"message": message, "finish_reason": "tool_calls" if len(sent) == 1 else "stop"}]}
        return {"status": "completed", "output": ([{"type": "function_call", "call_id": "c1",
            "name": "read_file", "arguments": '{"path":"a.py"}'}] if len(sent) == 1 else [
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ok"}]}])}

    monkeypatch.setattr(api_transport, "generate", generate)
    provider = ApiProvider("http://localhost:9/v1", "fixture", api_protocol=protocol, request_counter=count)
    tools = [ProviderToolDefinition("read_file", "read", {})]
    prompt = "Historical input: yesterday was 2024-05-22. Return fixed JSON."
    assert provider.send_turn(prompt, tools).tool_calls[0].id == "c1"
    first_history = copy.deepcopy(provider._messages)
    provider.send_tool_results([ProviderToolResult("c1", "exact historical result 2024-05-22")], tools)
    assert counted == sent
    key = "messages" if protocol == "openai-completions" else "input"
    for index, payload in enumerate(sent, 1):
        assert payload[key][-1] == {"role": "user", "content": f"<request_context>clock-{index}</request_context>"}
        assert json.dumps(payload).count("<request_context>") == 1
        assert prompt in json.dumps(payload)
    assert provider._messages[:len(first_history)] == first_history
    assert "<request_context>" not in json.dumps(provider._messages)


@pytest.mark.parametrize("purpose", ["review", "compaction"])
def test_other_generation_entries_have_clock_context(monkeypatch, purpose):
    payloads = []
    monkeypatch.setattr(request_context, "clock_context", lambda: "<request_context>aux</request_context>")
    monkeypatch.setattr(api_transport, "generate", lambda url, payload, *args, **kw:
        (payloads.append(copy.deepcopy(payload)) or {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}))
    provider = ApiProvider("http://localhost:9/v1", "fixture", purpose=purpose)
    if purpose == "compaction":
        provider = provider.fork_auxiliary()
    assert provider.send("Retain the historical date 2024-05-22.") == "ok"
    messages = payloads[0]["messages"]
    assert messages[-1] == {"role": "user", "content": "<request_context>aux</request_context>"}
    assert any(message.get("content") == "Retain the historical date 2024-05-22."
               for message in messages[:-1])
    assert "<request_context>" not in json.dumps(provider._messages)


def test_web_send_uses_same_clock_renderer_once(monkeypatch):
    from codey.providers import web_provider

    sent = []
    monkeypatch.setattr(request_context, "clock_context", lambda: "<request_context>web</request_context>")
    monkeypatch.setattr(web_provider, "run_web_send", lambda *a, **kw: kw["func"]())
    provider = web_provider.DeepSeekWebProvider(SimpleNamespace(page=object()))
    monkeypatch.setattr(provider.spec.driver, "chat", lambda page, text, **kw: (sent.append(text) or "ok"))
    for text in ("initial", "tool results", "review"):
        assert provider.send(text) == "ok"
        assert sent[-1] == "<request_context>web</request_context>\n\n" + text
