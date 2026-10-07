from __future__ import annotations

import json

import pytest

from codey.providers import api_transport
from codey.providers import error_classification as provider_errors
from codey.providers.api_provider import ApiProvider
from codey.providers.base import ProviderToolDefinition, ProviderToolResult, TurnFinish


class _Response:
    def __init__(self, body: dict) -> None:
        self.body = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, _size=None):
        return self.body


def test_local_provider_length_after_tool_result_allows_one_continuation(monkeypatch):
    provider = ApiProvider("http://127.0.0.1:9/v1", "gemma")
    provider._messages.append({"role": "assistant", "content": "", "tool_calls": [{"id": "call-1"}]})
    responses = iter([
        {"choices": [{"finish_reason": "length", "message": {"content": "Tests passed"}}]},
        {"choices": [{"finish_reason": "tool_calls", "message": {
            "content": "", "tool_calls": [{"id": "done-1", "type": "function", "function": {
                "name": "done", "arguments": '{"summary":"finished"}'
            }}]
        }}]},
    ])

    def fake_urlopen(request, timeout=None):
        del request, timeout
        return _Response(next(responses))

    monkeypatch.setattr(api_transport, "open_request", fake_urlopen)
    first = provider.send_tool_results([ProviderToolResult('call-1', 'tests passed')], [])
    assert first.text == "Tests passed"
    assert first.tool_calls == ()
    second = provider.send_turn(
        "The previous response was truncated. Continue the task or call done(summary).",
        [ProviderToolDefinition('done', '', {})],
    )
    assert second.tool_calls[0].name == "done"


def test_local_provider_does_not_promote_length_text_to_done(monkeypatch):
    provider = ApiProvider("http://127.0.0.1:9/v1", "gemma")
    provider._messages.append({"role": "assistant", "content": "", "tool_calls": [{"id": "call-1"}]})
    body = {"choices": [{"finish_reason": "length", "message": {"content": "done"}}]}
    monkeypatch.setattr(api_transport, "open_request", lambda request, timeout=None: _Response(body))
    turn = provider.send_tool_results([ProviderToolResult('call-1', 'tests passed')], [])
    assert turn.tool_calls == ()
    assert turn.text == "done"
    assert turn.finish is TurnFinish.OUTPUT_LIMIT


def test_local_provider_length_with_tool_calls_fails_closed(monkeypatch):
    provider = ApiProvider("http://127.0.0.1:9/v1", "gemma")
    body = {"choices": [{"finish_reason": "length", "message": {
        "content": "", "tool_calls": [{"id": "partial", "type": "function", "function": {
            "name": "done", "arguments": '{"summary":"partial"}'
        }}]
    }}]}
    monkeypatch.setattr(api_transport, "open_request", lambda request, timeout=None: _Response(body))
    with pytest.raises(provider_errors.OutputLengthError):
        provider.send_turn("finish", [ProviderToolDefinition('done', '', {})])
    assert provider._messages == []
