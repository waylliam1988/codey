from __future__ import annotations

import io
import json

import pytest

from codey.providers import api_messages, api_stream
from codey.providers.api_codec import GenerationSettings
from codey.providers.api_transport import GenerationUnknownError
from codey.providers.base import ProviderToolDefinition
from codey.providers.local_usage import parse_messages_usage
from codey.types.json_value import JsonObject, decode_json_object


def test_messages_places_tool_results_before_text_and_respects_thinking_constraints() -> None:
    tools = [ProviderToolDefinition("read_file", "Read a file", {"type": "object"})]
    settings = GenerationSettings("model", True, 0.3, True, None, "required", 4096)
    payload = api_messages.build_payload([], tools, settings)
    api_messages.finalize_messages(payload, [
        {"role": "tool", "tool_call_id": "first", "content": "one"},
        {"role": "user", "content": "Remember this"},
        {"role": "tool", "tool_call_id": "second", "content": "two"},
        {"role": "user", "content": "Current clock"},
    ])
    assert "temperature" not in payload
    assert payload["thinking"] == {"type": "enabled", "budget_tokens": 4095}
    assert payload["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": False}
    assert payload["messages"] == [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "first", "content": "one"},
        {"type": "tool_result", "tool_use_id": "second", "content": "two"},
        {"type": "text", "text": "Remember this"},
        {"type": "text", "text": "Current clock"},
    ]}]


def _stream(events: list[JsonObject]) -> JsonObject:
    raw = "".join("data: " + json.dumps(event) + "\n\n" for event in events).encode()
    return api_stream.read_sse(io.BytesIO(raw), deadline=float("inf"), cancelled=lambda: False)


def test_messages_sse_recognizes_duplicate_start_and_requires_terminal_tool_frame() -> None:
    start: JsonObject = {"type": "message_start", "message": {"type": "message", "id": "msg_one",
        "usage": {"input_tokens": 10, "output_tokens": 0}}}
    frames: list[JsonObject] = [start, start,
        {"type": "content_block_start", "index": 0, "content_block": {
            "type": "tool_use", "id": "call_1", "name": "read_file", "input": {}}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"path":'}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '"marker.txt"}'}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 2}},
        {"type": "message_stop"},
    ]
    message = _stream(frames)
    turn, _ = api_messages.decode_exchange(dict(message), text_only=False, text_decoder=None)
    call, = turn.tool_calls
    assert call.id == "call_1" and call.arguments == {"path": "marker.txt"}
    usage = parse_messages_usage(dict(message))
    assert usage is not None and usage.input_tokens == 10 and usage.output_tokens == 2
    with pytest.raises(GenerationUnknownError, match="terminal"):
        _stream(frames[:-1])
    with pytest.raises(GenerationUnknownError, match="duplicate starts"):
        incompatible = {**decode_json_object(start["message"]), "id": "msg_two"}
        _stream([start, {"type": "message_start", "message": incompatible}, *frames[2:]])
