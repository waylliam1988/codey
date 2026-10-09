"""Each physical request preserves usage before answer decoding, including failures."""
import json
from io import BytesIO

import pytest

from codey.providers import api_transport
from codey.providers.api_provider import ApiProvider


class Response(BytesIO):
    def __init__(self, body, stream=False):
        super().__init__(body)
        self.headers = {"Content-Type": "text/event-stream" if stream else "application/json"}


@pytest.mark.parametrize("stream", [False, True])
def test_usage_is_observed_before_decode_and_usage_only_stream_frames_are_delivered(monkeypatch, stream):
    events, records = [], []
    provider = ApiProvider("http://localhost:9/v1", "fixture", stream=stream)
    provider.usage_parser = lambda event: (events.append(event) or None)
    provider.on_usage = records.append
    body = {"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3}}
    raw = (b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n'
           b'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":3}}\n\n'
           b'data: [DONE]\n\n') if stream else json.dumps(body).encode()
    monkeypatch.setattr(api_transport, "open_request", lambda *args: Response(raw, stream))
    original = provider._codec.decode_exchange

    def decode(*args, **kwargs):
        assert records, "usage must be recorded before answer decoding"
        assert any("usage" in event for event in events)
        return original(*args, **kwargs)

    monkeypatch.setattr(provider._codec, 'decode_exchange', decode)
    assert provider.send("hello") == "ok"
    assert len(records) == 1


def test_decode_failure_still_records_request_and_next_request_has_a_new_identity(monkeypatch):
    provider = ApiProvider("http://localhost:9/v1", "fixture")
    records = []
    provider.on_usage = records.append
    monkeypatch.setattr(api_transport, "open_request", lambda *args: Response(b'{"choices":[]}'))
    for _ in range(2):
        with pytest.raises(RuntimeError, match="no choices"):
            provider.send("hello")
    assert len(records) == 2
    assert records[0].exchange_id != records[1].exchange_id


def test_stream_disconnect_records_unknown_usage_without_resending(monkeypatch):
    provider = ApiProvider("http://localhost:9/v1", "fixture", stream=True)
    records, sent = [], []
    provider.on_usage = records.append

    def open_request(*args):
        sent.append(args)
        return Response(b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n', True)

    monkeypatch.setattr(api_transport, "open_request", open_request)
    with pytest.raises(api_transport.GenerationUnknownError):
        provider.send("hello")
    assert len(sent) == len(records) == 1
    assert records[0].usage.input_tokens is None


def test_rebinding_during_counting_keeps_old_request_attached_to_original_run(monkeypatch):
    from codey.providers.token_accounting import RequestContextCount

    provider = ApiProvider("http://localhost:9/v1", "fixture")
    old, new = [], []
    provider.bind_usage("original", old.append)

    def count(payload, *, deadline):
        provider.bind_usage("next", new.append)
        return RequestContextCount(10, "tokenizer")

    provider.request_counter = count
    monkeypatch.setattr(api_transport, "open_request", lambda *a: Response(
        b'{"choices":[{"finish_reason":"stop","message":{"content":"ok"}}]}'))
    assert provider.send("hello") == "ok"
    assert len(old) == 1 and new == []
    assert old[0].connection_id == "original"
