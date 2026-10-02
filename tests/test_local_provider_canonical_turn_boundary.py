"""Exercise the actual local transport, including history and rejected batches."""

import json

import pytest

from codey.operations.kernel_protocol import normalize_turn
from codey.policies.task_policy import TaskPolicy
from codey.providers.local_openai import LocalOpenAIProvider


def _reply(monkeypatch, *, content, calls=None, finish="stop"):
    message = {"content": content}
    if calls is not None:
        message["tool_calls"] = calls
    monkeypatch.setattr(
        LocalOpenAIProvider, "_post_chat",
        lambda *args, **kwargs: {"choices": [{"finish_reason": finish, "message": message}]},
    )
    return LocalOpenAIProvider("http://127.0.0.1:9/v1", "gemma")


def test_native_text_frame_becomes_receiptable_call_and_canonical_history(monkeypatch):
    provider = _reply(monkeypatch, content='<|tool_call>call:tool:done{summary:"finished"}<tool_call|>')
    turn = provider.send_turn("finish", [])
    assert len(turn.tool_calls) == 1
    call = turn.tool_calls[0]
    assert call.name == "done"
    stored = provider._messages[-1]["tool_calls"][0]
    assert stored["id"] == call.id
    assert json.loads(stored["function"]["arguments"]) == call.arguments


def test_truncated_complete_looking_frame_never_becomes_call(monkeypatch):
    provider = _reply(monkeypatch, content='<|tool_call>call:tool:done{summary:"finished"}<tool_call|>', finish="length")
    turn = provider.send_turn("finish", [])
    assert turn.tool_calls == ()
    assert turn.raw["continuable_length"] is True
    assert "tool_calls" not in provider._messages[-1]


@pytest.mark.parametrize("bad", [None, {"function": {"name": "done", "arguments": "{}"}}])
def test_malformed_batch_cannot_fall_back_to_valid_done_in_content(monkeypatch, bad):
    provider = _reply(monkeypatch, content='{"tool":"done","args":{"summary":"finished"}}', calls=[bad])
    turn = provider.send_turn("finish", [])
    plan = normalize_turn(turn, policy=TaskPolicy(grants=frozenset({"control"})))
    assert plan.control is None
    assert plan.protocol_error


def test_coldstart_removes_unused_provider_entry_points():
    from codey.operations import task_loop
    from codey.providers import local_response_codec

    assert not hasattr(task_loop, "_local_length_continuation_prompt")
    assert not hasattr(local_response_codec, "normalize_ollama_response")


def test_native_valid_calls_take_precedence_over_text_frame(monkeypatch):
    provider = _reply(monkeypatch, content='<|tool_call>call:tool:done{summary:"ignored"}<tool_call|>', calls=[{
        "id": "read-1", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"a.py"}'},
    }])
    turn = provider.send_turn("read", [])
    assert [call.name for call in turn.tool_calls] == ["read_file"]
    assert provider._messages[-1]["tool_calls"][0]["id"] == "read-1"
