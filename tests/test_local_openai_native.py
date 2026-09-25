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
            return self._payload[:size]
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


def test_concurrent_sends_do_not_interleave_history(monkeypatch) -> None:
    import threading

    entered_network = threading.Event()
    release_network = threading.Event()

    def fake_complete(messages, *, timeout=None) -> str:
        del messages, timeout
        entered_network.set()
        assert release_network.wait(timeout=10.0)
        return "reply-A"

    provider = LocalOpenAIProvider(base_url="http://127.0.0.1:9/v1", model="qwen-test")
    monkeypatch.setattr(provider, "_complete", fake_complete)
    errors: list[BaseException] = []

    def send_b() -> None:
        try:
            provider.send("B")
        except BaseException as exc:
            errors.append(exc)

    thread_a = threading.Thread(target=lambda: provider.send("A"))
    thread_a.start()
    assert entered_network.wait(timeout=10.0)
    thread_b = threading.Thread(target=send_b)
    thread_b.start()
    thread_b.join(timeout=10.0)
    release_network.set()
    thread_a.join(timeout=10.0)
    assert any("busy" in str(exc) for exc in errors)
    roles = [m.get("role") for m in provider._messages]
    assert roles == ["user", "assistant"]
    assert provider._messages[0]["content"] == "A"


def test_close_discards_late_reply(monkeypatch) -> None:
    import threading

    entered_network = threading.Event()
    release_network = threading.Event()

    def fake_complete(messages, *, timeout=None) -> str:
        del messages, timeout
        entered_network.set()
        assert release_network.wait(timeout=10.0)
        return "late-reply"

    provider = LocalOpenAIProvider(base_url="http://127.0.0.1:9/v1", model="qwen-test")
    monkeypatch.setattr(provider, "_complete", fake_complete)
    result: list[str] = []

    def do_send() -> None:
        result.append(provider.send("old"))

    thread = threading.Thread(target=do_send)
    thread.start()
    assert entered_network.wait(timeout=10.0)
    provider.close()
    release_network.set()
    thread.join(timeout=10.0)
    assert result == ["late-reply"]
    assert provider._messages == []


def test_send_failure_leaves_history_unchanged(monkeypatch) -> None:
    import urllib.error

    import pytest

    provider = LocalOpenAIProvider(base_url="http://127.0.0.1:9/v1", model="qwen-test")
    body = {"choices": [{"finish_reason": "stop", "message": {"content": "first"}}]}
    _install_fake(monkeypatch, body)
    assert provider.send("first") == "first"
    before = [dict(message) for message in provider._messages]

    def failing_urlopen(request, timeout=None):
        raise urllib.error.URLError("network down")

    monkeypatch.setattr(local_module.urllib.request, "urlopen", failing_urlopen)
    with pytest.raises(RuntimeError, match="could not reach"):
        provider.send("second")
    # The unanswered user message must not linger for the next send.
    assert provider._messages == before

    _install_fake(monkeypatch, {"choices": [{"finish_reason": "stop", "message": {"content": "third"}}]})
    assert provider.send("third") == "third"
    assert [m.get("content") for m in provider._messages if m.get("role") == "user"] == ["first", "third"]


def test_tool_results_retry_posts_single_result(monkeypatch) -> None:
    import urllib.error

    provider = LocalOpenAIProvider(base_url="http://127.0.0.1:9/v1", model="qwen-test")
    provider._messages.append({"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]})
    before = [dict(message) for message in provider._messages]

    def failing_urlopen(request, timeout=None):
        raise urllib.error.URLError("network down")

    monkeypatch.setattr(local_module.urllib.request, "urlopen", failing_urlopen)
    try:
        provider.send_tool_results([{"tool_call_id": "call_1", "content": "out"}])
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected network failure")
    assert provider._messages == before

    body = {"choices": [{"finish_reason": "stop", "message": {"content": "done"}}]}
    seen = _install_fake(monkeypatch, body)
    turn = provider.send_tool_results([{"tool_call_id": "call_1", "content": "out"}])
    assert turn.text == "done"
    tool_msgs = [m for m in seen[0]["messages"] if m.get("role") == "tool"]
    assert [m.get("tool_call_id") for m in tool_msgs] == ["call_1"]
    assert len([m for m in provider._messages if m.get("role") == "tool"]) == 1


def test_stale_turn_skips_history_after_abandon(monkeypatch) -> None:
    import threading

    entered_network = threading.Event()
    release_network = threading.Event()

    def fake_complete_message(messages, tools=None, *, timeout=None) -> dict:
        del messages, tools, timeout
        entered_network.set()
        assert release_network.wait(timeout=10.0)
        return {"content": "late", "_finish_reason": "stop"}

    provider = LocalOpenAIProvider(base_url="http://127.0.0.1:9/v1", model="qwen-test")
    monkeypatch.setattr(provider, "_complete_message", fake_complete_message)
    result: list = []

    def do_send() -> None:
        result.append(provider.send_turn("old"))

    thread = threading.Thread(target=do_send)
    thread.start()
    assert entered_network.wait(timeout=10.0)
    provider.abandon_inflight()
    release_network.set()
    thread.join(timeout=10.0)
    assert len(result) == 1
    assert result[0].raw.get("stale_generation") is True
    assert provider._messages == []


def test_http_error_body_is_bounded(monkeypatch) -> None:
    import io
    import urllib.error

    import pytest

    seen_sizes: list[object] = []
    closed: list[bool] = []

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

    class TrackedError(urllib.error.HTTPError):
        def close(self) -> None:
            closed.append(True)
            super().close()

    provider = LocalOpenAIProvider(base_url="http://127.0.0.1:9/v1", model="qwen-test")

    def fake_urlopen(request, timeout=None):
        raise TrackedError(
            request.full_url, 500, "Server Error", {}, BoundedErrorIO(b"e" * 5000),
        )

    monkeypatch.setattr(local_module.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError):
        provider._post_chat([{"role": "user", "content": "hi"}])
    assert seen_sizes and seen_sizes[0] == 2001
    assert closed == [True]


def test_content_filter_is_explicit_error_and_leaves_history(monkeypatch) -> None:
    import pytest

    provider = LocalOpenAIProvider(base_url="http://127.0.0.1:9/v1", model="qwen-test")
    body = {
        "choices": [{
            "finish_reason": "content_filter",
            "message": {"content": ""},
        }]
    }
    _install_fake(monkeypatch, body)
    with pytest.raises(RuntimeError, match="content filtered"):
        provider.send("hello")
    assert provider._messages == []
    turn_body = {
        "choices": [{
            "finish_reason": "content_filter",
            "message": {"content": ""},
        }]
    }
    _install_fake(monkeypatch, turn_body)
    with pytest.raises(RuntimeError, match="content filtered"):
        provider.send_turn("hello")
    assert provider._messages == []


def test_malformed_tool_calls_shape_is_protocol_error(monkeypatch) -> None:
    import pytest

    provider = LocalOpenAIProvider(base_url="http://127.0.0.1:9/v1", model="qwen-test")
    body = {
        "choices": [{
            "finish_reason": "tool_calls",
            "message": {"content": "", "tool_calls": {"id": "call_1"}},
        }]
    }
    _install_fake(monkeypatch, body)
    with pytest.raises(RuntimeError, match="malformed tool_calls"):
        provider.send_turn("do work")
    assert provider._messages == []
    with pytest.raises(RuntimeError, match="malformed tool_calls"):
        local_module._parse_tool_calls({"content": "", "tool_calls": "nope"})
