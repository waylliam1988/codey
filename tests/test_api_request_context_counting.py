"""Final API request counting, admission, deadlines and template identity."""
import json
from io import BytesIO

import pytest

from codey.providers import api_transport
from codey.providers.api_provider import ApiProvider
from codey.providers.base import ProviderToolDefinition
from codey.providers.error_classification import ContextOverflowError


class Response(BytesIO):
    headers = {"Content-Type": "application/json"}


def test_exact_counter_gates_request_and_receives_template_tools_and_full_history(monkeypatch):
    provider = ApiProvider("http://localhost:9/v1", "fixture", thinking_enabled=False,
                           context_window_tokens=200, context_reserve_tokens=50, context_keep_recent_tokens=60)
    provider._messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "old"}]
    counted, sent = [], []

    def count(payload, *, deadline):
        from codey.providers.token_accounting import RequestContextCount
        counted.append(json.loads(json.dumps(payload)))
        return RequestContextCount(151, "tokenizer")

    provider.request_counter = count
    monkeypatch.setattr(api_transport, "open_request", lambda request, timeout: (sent.append(request) or Response(b'{"choices":[{"finish_reason":"stop","message":{"content":"ok"}}]}')))
    with pytest.raises(ContextOverflowError):
        provider.send_turn("new", [ProviderToolDefinition("read", "read file", {})])
    assert sent == []
    assert counted[-1]["chat_template_kwargs"] == {"enable_thinking": False}
    assert counted[-1]["tools"][0]["function"]["name"] == "read"
    assert provider._messages[-1]["content"] == "old"


def test_payload_that_was_counted_is_the_payload_sent(monkeypatch):
    provider = ApiProvider("http://localhost:9/v1", "fixture", thinking_enabled=True)
    counted, sent = [], []

    def count(payload, *, deadline):
        from codey.providers.token_accounting import RequestContextCount
        counted.append(json.loads(json.dumps(payload)))
        return RequestContextCount(88, "tokenizer")

    def open_request(request, timeout):
        sent.append(json.loads(request.data))
        return Response(b'{"choices":[{"finish_reason":"stop","message":{"content":"ok"}}]}')

    provider.request_counter = count
    monkeypatch.setattr(api_transport, "open_request", open_request)
    assert provider.send("hello") == "ok"
    assert counted == sent


def test_confirmed_tokenizer_failure_never_falls_back_to_estimate_or_generation(monkeypatch):
    provider = ApiProvider("http://localhost:9/v1", "fixture")
    sent = []

    def count(payload, *, deadline):
        raise RuntimeError("tokenizer unavailable")

    provider.request_counter = count
    monkeypatch.setattr(api_transport, "open_request", lambda *args: (sent.append(args) or Response(b'{"choices":[{"finish_reason":"stop","message":{"content":"ok"}}]}')))
    with pytest.raises(RuntimeError, match="tokenizer unavailable"):
        provider.send("hello")
    assert not sent


def test_counting_that_exhausts_deadline_does_not_start_generation(monkeypatch):
    from codey.providers import api_provider
    from codey.providers.error_classification import RequestPrepError
    from codey.providers.token_accounting import RequestContextCount

    clock = {"now": 100.0}
    monkeypatch.setattr(api_provider.time, "monotonic", lambda: clock["now"])
    provider = ApiProvider("http://localhost:9/v1", "fixture")
    sent = []

    def count(payload, *, deadline):
        clock["now"] = deadline + 1
        return RequestContextCount(10, "tokenizer")

    provider.request_counter = count
    monkeypatch.setattr(api_transport, "open_request", lambda *args: (sent.append(args) or
                        Response(b'{"choices":[{"finish_reason":"stop","message":{"content":"ok"}}]}')))
    with pytest.raises(RequestPrepError, match="deadline"):
        provider.send("hello", timeout=10)
    assert sent == []
