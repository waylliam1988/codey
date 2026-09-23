from __future__ import annotations

import json

from codey.providers import local_openai as local_module
from codey.providers.local_openai import LocalOpenAIProvider


class _FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
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
