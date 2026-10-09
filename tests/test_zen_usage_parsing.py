"""Zen-owned usage parsing through real factories and shared request lifecycles."""
import json
from io import BytesIO
from types import SimpleNamespace

import pytest

from codey.providers import api_transport
from codey.providers.zen import connection
from codey.providers.zen.catalog import ZenModel
from codey.runtime.core.api_selection import ApiRunSelection


def zen_provider(monkeypatch, protocol, stream=False):
    monkeypatch.setattr(connection, "catalog", lambda: SimpleNamespace(
        require=lambda *a, **k: ZenModel("fixture", "Fixture", protocol, 32768, 8192),
        access=SimpleNamespace(record_plain_success=lambda *a: None)))
    return connection.open_selection(ApiRunSelection("zen", connection.CONNECTION_REVISION, "fixture", protocol, True,
                                                     stream=stream, tool_choice="auto", output_tokens=8192))


class Response(BytesIO):
    def __init__(self, raw, stream=False):
        super().__init__(raw)
        self.headers = {"Content-Type": "text/event-stream" if stream else "application/json"}


@pytest.mark.parametrize("protocol", ["openai-completions", "openai-responses"])
@pytest.mark.parametrize("stream", [False, True])
def test_factory_parses_protocol_usage_and_does_not_sum_repeated_snapshots(monkeypatch, protocol, stream):
    provider = zen_provider(monkeypatch, protocol, stream)
    records, sent = [], []
    provider.bind_usage("zen", records.append)
    if protocol == "openai-completions":
        usage = {"prompt_tokens": 100, "completion_tokens": 50, "prompt_tokens_details": {"cached_tokens": 80},
                 "completion_tokens_details": {"reasoning_tokens": 30}}
        body = {"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}], "usage": usage}
        events = [{"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]},
                  {"choices": [], "usage": usage}, {"choices": [], "usage": usage}]
    else:
        usage = {"input_tokens": 100, "output_tokens": 50, "input_tokens_details": {"cached_tokens": 80},
                 "output_tokens_details": {"reasoning_tokens": 30}}
        body = {"status": "completed", "output": [{"type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": "ok"}]}], "usage": usage}
        events = [{"type": "response.completed", "response": body}]

    def open_request(request, timeout):
        sent.append(json.loads(request.data))
        raw = b"".join(b"data: " + json.dumps(event).encode() + b"\n\n" for event in events) + b"data: [DONE]\n\n"
        return Response(raw if stream else json.dumps(body).encode(), stream)

    monkeypatch.setattr(api_transport, "open_request", open_request)
    assert provider.send("hello") == "ok"
    assert len(records) == 1
    assert records[0].usage.input_tokens == 100
    assert records[0].usage.output_tokens == 50
    assert records[0].usage.cached_input_tokens == 80
    assert records[0].usage.reasoning_output_tokens == 30
    if protocol == "openai-completions" and stream:
        assert sent[0]["stream_options"] == {"include_usage": True}


def test_internal_tool_refusal_closure_accounts_for_each_physical_generation(monkeypatch):
    provider = zen_provider(monkeypatch, "openai-completions")
    records = []
    provider.bind_usage("zen", records.append)
    bodies = iter([
        {"choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [
            {"id": "read-one", "function": {"name": "read", "arguments": "{}"}}]}}],
         "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
        {"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}],
         "usage": {"prompt_tokens": 20, "completion_tokens": 7}},
    ])
    monkeypatch.setattr(api_transport, "open_request", lambda *a: Response(json.dumps(next(bodies)).encode()))
    assert provider.send("hello") == "ok"
    assert len(records) == 2
    assert sum(record.usage.input_tokens for record in records) == 30
    assert records[0].exchange_id != records[1].exchange_id


@pytest.mark.parametrize("usage,expected,status", [
    (None, None, "missing"), ({"prompt_tokens": 0, "completion_tokens": 0}, 0, "reported"),
    ({"prompt_tokens": True, "completion_tokens": 5}, None, "invalid"),
    ({"prompt_tokens": 10}, 10, "reported"),
    ({"prompt_tokens": 10, "completion_tokens": 5, "prompt_tokens_details": None}, 10, "reported"),
])
def test_missing_partial_invalid_and_null_optional_usage_never_invalidate_answer(monkeypatch, usage, expected, status):
    provider = zen_provider(monkeypatch, "openai-completions")
    records = []
    provider.bind_usage("zen", records.append)
    body = {"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}], "usage": usage}
    monkeypatch.setattr(api_transport, "open_request", lambda *a: Response(json.dumps(body).encode()))
    assert provider.send("hello") == "ok"
    assert records[0].usage.input_tokens == expected
    assert records[0].usage_status == status
