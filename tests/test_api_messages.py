from __future__ import annotations

import io
import json
from dataclasses import replace

import pytest

from codey.providers import api_messages, api_stream
from codey.providers.api_codec import GenerationSettings
from codey.providers.api_metering import UsageCollector
from codey.providers.api_provider import ApiProvider
from codey.providers.api_transport import GenerationUnknownError
from codey.providers.base import ProviderToolDefinition, TurnFinish
from codey.providers.error_classification import OutputLengthError
from codey.providers.local_usage import parse_messages_usage
from codey.runtime.core.protocol_window import ANTHROPIC_BLOCKS
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
    assert payload["thinking"] == {"type": "enabled", "budget_tokens": 2048}
    assert payload["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": False}
    assert payload["messages"] == [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "first", "content": "one"},
        {"type": "tool_result", "tool_use_id": "second", "content": "two"},
        {"type": "text", "text": "Remember this"},
        {"type": "text", "text": "Current clock"},
    ]}]
    small = api_messages.build_payload([], tools, replace(settings, output_tokens=1500))
    assert "thinking" not in small and small["temperature"] == 0.3
    minimum = api_messages.build_payload([], tools, replace(settings, output_tokens=2048))
    assert minimum["thinking"] == {"type": "enabled", "budget_tokens": 1024}


def _stream(events: list[JsonObject], collector: UsageCollector | None = None) -> JsonObject:
    raw = "".join("data: " + json.dumps(event) + "\n\n" for event in events).encode()
    return api_stream.read_sse(io.BytesIO(raw), deadline=float("inf"), cancelled=lambda: False,
                               on_event=collector.observe if collector is not None else lambda _event: None)


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


@pytest.mark.parametrize(("arguments", "reason", "block_closed"), [
    ("", "tool_use", True),
    ('{"path":', "max_tokens", True),
    ('{"path":', "max_tokens", False),
    ('{"path":', "tool_use", True),
])
def test_messages_stream_handles_empty_and_truncated_tool_input(
    arguments: str, reason: str, block_closed: bool,
) -> None:
    start: JsonObject = {"type": "message_start", "message": {"type": "message", "id": "same",
        "usage": {"input_tokens": 10, "cache_read_input_tokens": 3, "cache_creation_input_tokens": 2}}}
    frames: list[JsonObject] = [start,
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": "Checking."}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_start", "message": {"type": "message", "id": "same"}},
        {"type": "content_block_start", "index": 1, "content_block": {
            "type": "tool_use", "id": "call_1", "name": "browser_snapshot", "input": {}}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": arguments}},
    ]
    if block_closed:
        frames.append({"type": "content_block_stop", "index": 1})
    collector = UsageCollector(parse_messages_usage)
    with pytest.raises(GenerationUnknownError, match="terminal"):
        _stream(frames, collector)
    assert collector.usage.input_tokens == 15
    assert collector.usage.cached_input_tokens == 3 and collector.usage.cache_write_input_tokens == 2
    frames.extend([{"type": "message_delta", "delta": {"stop_reason": reason}}, {"type": "message_stop"}])
    if reason == "tool_use" and arguments:
        with pytest.raises(GenerationUnknownError, match="malformed Messages stream"):
            _stream(frames)
        return
    message = dict(_stream(frames))
    turn, stored = api_messages.decode_exchange(message, text_only=False, text_decoder=None)
    assert turn.text == "Checking."
    if reason == "max_tokens":
        assert turn.finish is TurnFinish.OUTPUT_LIMIT and not turn.tool_calls and stored is None
        with pytest.raises(OutputLengthError) as error:
            api_messages.decode_exchange(message, text_only=True, text_decoder=None)
        assert error.value.turn == turn
    else:
        call, = turn.tool_calls
        assert call.arguments == {}


def test_messages_applies_text_decoder_and_omits_openai_response_format() -> None:
    blocks = [{"type": "thinking", "thinking": "private", "signature": "signed"},
              {"type": "text", "text": " raw "}]
    body: dict[str, object] = {"type": "message", "stop_reason": "end_turn",
                               "content": blocks}
    turn, stored = api_messages.decode_exchange(body, text_only=False, text_decoder=str.strip)
    assert turn.text == "raw" and stored == [{"role": "assistant", "content": "raw", ANTHROPIC_BLOCKS: blocks}]
    provider = ApiProvider("http://localhost:5001/v1", "fixture", api_protocol="anthropic-messages")
    try:
        payload = provider._payload([{"role": "user", "content": "Respond"}], None,
            GenerationSettings("fixture", False, 0, None, None, "auto", 2048, text_tool_json=True))
    finally:
        provider.close()
    assert "response_format" not in payload
