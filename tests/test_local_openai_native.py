from __future__ import annotations

import json

from codey.providers import local_openai as local_module
from codey.providers.local_openai import LocalOpenAIProvider


class _FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self.read_sizes: list[object] = []

    def read(self, size: int | None = None) -> bytes:
        self.read_sizes.append(size)
        if size is not None and size >= 0:
            return self._payload[: size + 1] if len(self._payload) > size else self._payload
        return self._payload

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _install_fake(monkeypatch, body: dict) -> list[dict]:
    seen: list[dict] = []

    def fake_urlopen(request, timeout=None):
        seen.append(json.loads(request.data.decode("utf-8")))
        return _FakeResponse(json.dumps(body).encode("utf-8"))

    monkeypatch.setattr(local_module.urllib.request, "urlopen", fake_urlopen)
    return seen


def test_send_turn_posts_tools_and_records_tool_calls(monkeypatch) -> None:
    provider = LocalOpenAIProvider(base_url="http://127.0.0.1:9/v1", model="qwen-test")
    body = {
        "choices": [{
            "finish_reason": "tool_calls",
            "message": {
                "content": "",
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read", "arguments": '{"path":"app.py"}'},
                }],
            },
        }]
    }
    seen = _install_fake(monkeypatch, body)
    turn = provider.send_turn("read app", [{"type": "function", "function": {"name": "read"}}])
    assert seen[0]["tools"] == [{"type": "function", "function": {"name": "read"}}]
    assert seen[0]["tool_choice"] == "auto"
    assert turn.tool_calls[0].id == "call_1"
    assert turn.tool_calls[0].name == "read"
    # Assistant message with tool_calls must be retained for chaining.
    assert provider._messages[-1]["tool_calls"][0]["id"] == "call_1"


def test_send_tool_results_posts_role_tool(monkeypatch) -> None:
    provider = LocalOpenAIProvider(base_url="http://127.0.0.1:9/v1", model="qwen-test")
    provider._messages.append({"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]})
    body = {"choices": [{"finish_reason": "stop", "message": {"content": "done"}}]}
    seen = _install_fake(monkeypatch, body)
    turn = provider.send_tool_results(
        [{"tool_call_id": "call_1", "content": "file contents"}],
        [{"type": "function", "function": {"name": "read"}}],
    )
    tool_msgs = [m for m in seen[0]["messages"] if m.get("role") == "tool"]
    assert tool_msgs and tool_msgs[0]["tool_call_id"] == "call_1"
    assert turn.text == "done"


def test_length_finish_reason_raises_not_parsed(monkeypatch) -> None:
    provider = LocalOpenAIProvider(base_url="http://127.0.0.1:9/v1", model="qwen-test")
    body = {"choices": [{"finish_reason": "length", "message": {"content": '{"tool":'}}]}
    _install_fake(monkeypatch, body)
    try:
        provider.send("hello")
    except Exception as exc:
        assert type(exc).__name__ in ("OutputLengthError", "ContextOverflowError")
    else:
        raise AssertionError("expected truncation error")


def test_unsupported_tools_hint_points_at_canonical_shape(monkeypatch) -> None:
    import io
    import urllib.error

    import pytest

    provider = LocalOpenAIProvider(base_url="http://127.0.0.1:9/v1", model="qwen-test")
    detail = b'{"error": {"message": "unsupported parameter: tool_choice"}}'

    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 400, "Bad Request", {}, io.BytesIO(detail))

    monkeypatch.setattr(local_module.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError, match="rejected native tools") as excinfo:
        provider._post_chat([{"role": "user", "content": "hi"}], [{"type": "function"}])
    message = str(excinfo.value)
    assert "NATIVE_TOOLS=0" in message
    assert '"native_tools_mode":"off"' in message
    assert '{"native_tools": false}' not in message


def test_chat_response_uses_bounded_read(monkeypatch) -> None:
    from codey.providers.local_openai import _CHAT_RESPONSE_MAX_BYTES

    responses: list[_FakeResponse] = []

    def fake_urlopen(request, timeout=None):
        body = {"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]}
        fake = _FakeResponse(json.dumps(body).encode("utf-8"))
        responses.append(fake)
        return fake

    monkeypatch.setattr(local_module.urllib.request, "urlopen", fake_urlopen)
    provider = LocalOpenAIProvider(base_url="http://127.0.0.1:9/v1", model="qwen-test")
    assert provider.send("hi") == "ok"
    assert responses and responses[0].read_sizes
    assert responses[0].read_sizes[0] == _CHAT_RESPONSE_MAX_BYTES + 1


def test_chat_response_over_limit_is_rejected(monkeypatch) -> None:
    import pytest

    from codey.providers import local_openai as provider_module

    monkeypatch.setattr(provider_module, "_CHAT_RESPONSE_MAX_BYTES", 16)

    def fake_urlopen(request, timeout=None):
        return _FakeResponse(b"x" * 32)

    monkeypatch.setattr(local_module.urllib.request, "urlopen", fake_urlopen)
    provider = LocalOpenAIProvider(base_url="http://127.0.0.1:9/v1", model="qwen-test")
    with pytest.raises(RuntimeError, match="exceeded 16 bytes"):
        provider.send("hi")


def test_http_error_body_is_bounded(monkeypatch) -> None:
    import io
    import urllib.error

    import pytest

    seen_sizes: list[object] = []

    class BoundedErrorIO(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            seen_sizes.append(size)
            if size is not None and size >= 0:
                data = super().read(size)
                # Simulate an unbounded body: always claim one more byte.
                if len(data) == size:
                    return data
                return data
            return super().read()

    provider = LocalOpenAIProvider(base_url="http://127.0.0.1:9/v1", model="qwen-test")

    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url, 500, "Server Error", {}, BoundedErrorIO(b"e" * 5000),
        )

    monkeypatch.setattr(local_module.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError):
        provider._post_chat([{"role": "user", "content": "hi"}])
    assert seen_sizes and seen_sizes[0] == 2001
