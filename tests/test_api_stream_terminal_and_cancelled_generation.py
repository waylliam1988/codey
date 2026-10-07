"""Streams require terminal framing; cancelled generations never return calls."""
import io
import json
import urllib.error
from unittest.mock import patch

import pytest

from codey.providers import api_transport
from codey.providers.api_provider import ApiProvider


def test_chat_finish_reason_without_done_frame_is_unknown():
    event = {"choices": [{"delta": {"content": "unfinished"}, "finish_reason": "stop"}]}
    raw = ("data: " + json.dumps(event) + "\n\n").encode()
    with pytest.raises(api_transport.GenerationUnknownError):
        api_transport.read_sse(io.BytesIO(raw), deadline=float("inf"), cancelled=lambda: False)


@pytest.mark.parametrize("protocol", ["openai-completions", "openai-responses"])
def test_cancelled_generation_does_not_return_executable_calls(protocol):
    provider = ApiProvider("http://model.test/v1", "fixture", api_protocol=protocol)

    def late_reply(*a, **k):
        provider.abandon_inflight()
        if protocol == "openai-responses":
            return {"status": "completed", "output": [{"type": "function_call", "call_id": "old", "name": "done", "arguments": "{}"}]}
        return {"choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [{"id": "old", "function": {"name": "done", "arguments": "{}"}}]}}]}

    with patch.object(api_transport, "generate", late_reply), pytest.raises(api_transport.GenerationUnknownError):
        provider.send_turn("fixture")
    assert provider._messages == []


def test_connection_refusal_is_known_not_sent_without_replay(monkeypatch):
    calls = []

    def refused(*a, **k):
        calls.append(1)
        raise urllib.error.URLError(ConnectionRefusedError("fixture refused"))

    monkeypatch.setattr(api_transport, "open_request", refused)
    with pytest.raises(RuntimeError) as error:
        api_transport.generate("http://fixture.test/v1", {}, {}, timeout=1, observe=lambda **k: None)
    assert getattr(error.value, "provider_failure_kind", "") == "not_submitted"
    assert len(calls) == 1


@pytest.mark.parametrize("protocol", ["openai-completions", "openai-responses"])
def test_abandon_closes_the_open_transport_and_discards_reply(monkeypatch, protocol):
    provider = ApiProvider("http://fixture.test/v1", "fixture", api_protocol=protocol)
    from types import SimpleNamespace

    closed = []
    active = SimpleNamespace(close=lambda: closed.append(True))

    def send(*args, **kwargs):
        kwargs["opened"](active)
        provider.abandon_inflight()
        return {"choices": [{"finish_reason": "stop", "message": {"content": "late"}}], "status": "completed", "output": []}

    monkeypatch.setattr(api_transport, "generate", send)
    with pytest.raises(api_transport.GenerationUnknownError):
        provider.send_turn("fixture")
    assert closed == [True]
    assert not provider.has_transport_history
