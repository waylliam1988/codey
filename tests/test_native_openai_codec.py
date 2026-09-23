from __future__ import annotations

from codey.protocols.native_openai import NativeOpenAIToolCodec
from codey.providers.base import AssistantTurn, ProviderToolCall


def _turn(text: str = "", calls: tuple = ()) -> AssistantTurn:
    return AssistantTurn(text=text, tool_calls=tuple(calls))


def test_native_read_lowered_to_canonical() -> None:
    codec = NativeOpenAIToolCodec()
    turn = _turn(calls=(ProviderToolCall(id="call_1", name="read", arguments={"path": "app.py"}),))
    plan = codec.parse_turn(turn)
    assert not plan.protocol_error
    assert [c.name for c in plan.calls] == ["read"]
    assert plan.calls[0].args["path"] == "app.py"
    assert plan.calls[0].call_id == "call_1"
    assert plan.control is not None and plan.control.kind == "continue"


def test_native_bad_args_become_protocol_error() -> None:
    codec = NativeOpenAIToolCodec()
    turn = _turn(calls=(ProviderToolCall(id="call_2", name="read", arguments={"offset": "x"}),))
    plan = codec.parse_turn(turn)
    # Missing required path -> validation error, no execution.
    assert plan.protocol_error
    assert not plan.calls


def test_native_unknown_tool_becomes_protocol_error() -> None:
    codec = NativeOpenAIToolCodec()
    turn = _turn(calls=(ProviderToolCall(id="call_3", name="nope", arguments={}),))
    plan = codec.parse_turn(turn)
    assert plan.protocol_error
    assert plan.protocol_error_kind == "unknown_tool"


def test_native_text_falls_back_to_json() -> None:
    codec = NativeOpenAIToolCodec()
    plan = codec.parse_turn(_turn(text='{"tool":"read_file","args":{"path":"app.py"}}'))
    assert [c.name for c in plan.calls] == ["read"]


def test_tool_messages_keep_called_ids() -> None:
    from codey.runtime.core.models import ToolCall, ToolResult

    ok = ToolResult(call=ToolCall(name="read", args={"path": "a"}, call_id="call_1"), model_text="hi")
    messages = NativeOpenAIToolCodec.tool_messages([ok])
    assert messages == [{"role": "tool", "tool_call_id": "call_1", "content": "hi"}]


def test_tool_messages_missing_call_id_fails_closed() -> None:
    import pytest

    from codey.protocols.native_openai import NativeToolResultError
    from codey.runtime.core.models import ToolCall, ToolResult

    ok = ToolResult(call=ToolCall(name="read", args={"path": "a"}, call_id="call_1"), model_text="hi")
    missing = ToolResult(call=ToolCall(name="read", args={"path": "b"}), model_text="hi")
    with pytest.raises(NativeToolResultError):
        NativeOpenAIToolCodec.tool_messages([ok, missing])
