"""Thinking must survive a cold reload independently of tool result text."""

import io
import json

import pytest

from codey.app.context import AppContext
from codey.durable.provider_response import tool_call_message
from codey.providers import api_chat, api_stream
from codey.providers.base import AssistantTurn, ProviderToolCall
from codey.providers.response_record import encode_reply
from codey.runtime.observe.events import RunEvent, run_event_ui_payload


@pytest.mark.parametrize("event_kind", ["turn", "reasoning"])
def test_thinking_display_survives_restart_without_entering_model_context(tmp_path, event_kind):
    ctx = AppContext(tmp_path, start_scheduler=False)
    try:
        ctx.durable.admit_input(conversation_id="s", request_id="one", task_id="r", text="inspect", provider_snapshot={})
        before = ctx.durable.context_snapshot("s", token_budget=10000).messages
        event = RunEvent(event_kind, turn=1, reasoning="Inspect the actual source before concluding.")
        payload = run_event_ui_payload("r", "s", event)
        ctx.emit(payload)
        ctx.emit(payload)
        rows = [item for item in ctx.durable.history_page("s")["items"] if item["type"] in {"thinking", "turn"}]
        assert len(rows) == 1
        assert rows[0].get("reasoning", rows[0].get("text")) == event.reasoning
        assert ctx.durable.context_snapshot("s", token_budget=10000).messages == before
    finally:
        ctx.close()
    cold = AppContext(tmp_path, start_scheduler=False)
    try:
        assert any(item.get("reasoning", item.get("text")) == event.reasoning
                   for item in cold.durable.history_page("s")["items"])
    finally:
        cold.close()


def test_native_tool_context_projection_preserves_reasoning(tmp_path):
    from codey.durable import DurableSession, MemoryDurableStore

    reply = AssistantTurn(reasoning="Inspect first.", text="Opening the source.", tool_calls=(
        ProviderToolCall("one", "read_file", {"path": "app.py"}),))
    message = tool_call_message(encode_reply(reply))
    assert message.get("reasoning_content") == reply.reasoning
    durable = DurableSession(MemoryDurableStore())
    try:
        durable.create_conversation("s")
        durable.append_message("s", "tool_call", message)
        durable.append_message("s", "tool_result", {"call_id": "one", "content": "source"})
        restored = durable.context_snapshot("s", token_budget=10000).messages[0]
        assert restored.get("reasoning_content") == reply.reasoning
        assert restored.get("content") == reply.text
    finally:
        durable.close()


@pytest.mark.parametrize("field", ["reasoning_content", "reasoning", "thinking"])
@pytest.mark.parametrize("value", ["Inspect first.", {"text": "malformed"}, 42, ["bad"]])
def test_stream_reasoning_has_the_same_alias_and_type_rules_as_nonstream(field, value):
    events = [{"choices": [{"delta": {field: value, "content": "Answer"}, "finish_reason": "stop"}]}]
    raw = b"".join(b"data: " + json.dumps(event).encode() + b"\n\n" for event in events) + b"data: [DONE]\n\n"
    body = api_stream.read_sse(io.BytesIO(raw), deadline=float("inf"), cancelled=lambda: False)
    turn, _ = api_chat.decode_exchange(body, text_only=False, text_decoder=None)
    assert turn.text == "Answer"
    assert turn.reasoning == (value if isinstance(value, str) else "")
