"""Anthropic Messages encoding over the existing provider history and tool pairs."""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace

from codey.providers.api_chat import (
    checkpoint_item as checkpoint_item,
)
from codey.providers.api_chat import (
    closed_spans as closed_spans,
)
from codey.providers.api_chat import (
    encode_context as encode_context,
)
from codey.providers.api_chat import (
    encode_results as encode_results,
)
from codey.providers.api_chat import (
    pending_calls as pending_calls,
)
from codey.providers.api_chat import (
    prepare as prepare,
)
from codey.providers.api_chat import (
    validate_view as validate_view,
)
from codey.providers.api_codec import GenerationSettings, allows_native_parallel_reads
from codey.providers.base import AssistantTurn, ProviderToolCall, ProviderToolDefinition, TurnFinish
from codey.providers.error_classification import OutputLengthError
from codey.providers.tool_images import materialize_messages
from codey.toolchain.tool_spec import thaw_schema_value
from codey.types.json_value import JsonObject, decode_json_object, parse_json_object, require_json_string
from codey.types.protocol_window import ANTHROPIC_BLOCKS

endpoint = "messages"


def _blocks(value: object) -> list[JsonObject]:
    if isinstance(value, str):
        return [{"type": "text", "text": value}] if value else []
    if not isinstance(value, list):
        return []
    blocks: list[JsonObject] = []
    for raw in value:
        row = decode_json_object(raw)
        if row.get("type") == "image_url":
            image = decode_json_object(row.get("image_url"))
            url = require_json_string(image.get("url"), "image URL")
            prefix, marker, data = url.partition(";base64,")
            if not marker or not prefix.startswith("data:image/") or not data:
                raise ValueError("Messages attachments require a base64 image")
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": prefix[5:], "data": data}})
        else:
            blocks.append(row)
    return blocks


def _assistant_blocks(message: dict[str, object]) -> list[JsonObject]:
    stored = message.get(ANTHROPIC_BLOCKS)
    if isinstance(stored, list):
        return _blocks(stored)
    blocks = _blocks(message.get("content"))
    calls = message.get("tool_calls")
    if isinstance(calls, list):
        for item in calls:
            call = decode_json_object(item)
            function = decode_json_object(call.get("function"))
            blocks.append({"type": "tool_use", "id": require_json_string(call.get("id"), "tool id"),
                "name": require_json_string(function.get("name"), "tool name"),
                "input": parse_json_object(require_json_string(function.get("arguments"), "tool arguments"))})
    return blocks


def build_payload(items: list[dict[str, object]], tools: list[ProviderToolDefinition] | None,
                  settings: GenerationSettings) -> dict[str, object]:
    max_tokens = settings.output_tokens or 4096
    thinking = settings.thinking is True and max_tokens >= 2048
    payload: dict[str, object] = {"model": settings.model, "stream": settings.stream, "max_tokens": max_tokens,
        "messages": materialize_messages(items, protocol="anthropic-messages", pending_calls=pending_calls)}
    if thinking:
        payload["thinking"] = {"type": "enabled", "budget_tokens": min(4096, max_tokens // 2)}
    else:
        payload["temperature"] = settings.temperature
    if tools:
        payload["tools"] = [{"name": tool.name, "description": tool.description,
                             "input_schema": thaw_schema_value(tool.parameters)} for tool in tools]
        payload["tool_choice"] = {"type": "any" if settings.choice == "required" and not thinking else "auto",
                                  "disable_parallel_tool_use": not allows_native_parallel_reads(tools)}
    return payload


def finalize_messages(payload: dict[str, object], items: list[dict[str, object]]) -> None:
    messages: list[tuple[str, list[JsonObject]]] = []
    system: list[str] = []
    for item in items:
        role = require_json_string(item.get("role"), "message role")
        if role in ("system", "developer"):
            system.append(require_json_string(item.get("content"), "system message"))
            continue
        tool_result = role == "tool"
        if tool_result:
            content: list[JsonObject] = [{"type": "tool_result", "tool_use_id": require_json_string(item.get("tool_call_id"), "tool result id"),
                "content": require_json_string(item.get("content", ""), "tool result")}]
            role = "user"
        else:
            content = _assistant_blocks(item) if role == "assistant" else _blocks(item.get("content"))
        if not content:
            continue
        if messages and messages[-1][0] == role:
            preceding = messages[-1][1]
            if tool_result:
                index = next((i for i, block in enumerate(preceding) if block.get("type") != "tool_result"), len(preceding))
                preceding[index:index] = content
            else:
                preceding.extend(content)
        else:
            messages.append((role, content))
    payload["messages"] = [{"role": role, "content": blocks} for role, blocks in messages]
    if system:
        payload["system"] = "\n\n".join(system)


def decode_exchange(body: dict[str, object], *, text_only: bool,
                    text_decoder: Callable[[str], str | AssistantTurn] | None,
                    ) -> tuple[AssistantTurn, list[dict[str, object]] | None]:
    if body.get("type") != "message" or not isinstance(body.get("content"), list):
        raise ValueError("invalid Messages response")
    blocks = _blocks(body["content"])
    limited = body.get("stop_reason") == "max_tokens"
    calls: list[ProviderToolCall] = []
    visible: list[str] = []
    reasoning: list[str] = []
    has_tool_blocks = False
    for block in blocks:
        if block.get("type") == "tool_use":
            has_tool_blocks = True
            if not limited:
                calls.append(ProviderToolCall(require_json_string(block.get("id"), "tool id"),
                    require_json_string(block.get("name"), "tool name"), decode_json_object(block.get("input"))))
        elif block.get("type") == "text":
            visible.append(require_json_string(block.get("text"), "message text"))
        elif block.get("type") == "thinking":
            reasoning.append(require_json_string(block.get("thinking"), "thinking"))
        elif block.get("type") != "redacted_thinking":
            raise ValueError("unsupported Messages content block")
    if body.get("stop_reason") in {"refusal", "pause_turn"}:
        raise ValueError("Messages cannot continue this response")
    if text_only and calls:
        raise ValueError("unexpected native tool call in a text-only response")
    if len({call.id for call in calls}) != len(calls):
        raise ValueError("duplicate Messages tool call ID")
    text = "\n".join(visible)
    turn = AssistantTurn(text=text, reasoning="\n".join(reasoning), tool_calls=tuple(calls),
        raw={ANTHROPIC_BLOCKS: blocks} if calls else {},
        finish=TurnFinish.OUTPUT_LIMIT if limited else TurnFinish.COMPLETE)
    if limited:
        if text_only:
            raise OutputLengthError("Messages output truncated", turn=turn)
        return turn, None
    stored: dict[str, object] = {"role": "assistant", "content": text, ANTHROPIC_BLOCKS: blocks}
    if not text_only and not has_tool_blocks and text_decoder is not None:
        normalized = text_decoder(text)
        if isinstance(normalized, AssistantTurn):
            if normalized.finish is TurnFinish.PROTOCOL_ERROR:
                return normalized, None
            turn = replace(normalized, reasoning=turn.reasoning or normalized.reasoning)
        else:
            turn = replace(turn, text=normalized)
        stored["content"] = turn.text
        if turn.tool_calls:
            stored.pop(ANTHROPIC_BLOCKS)
    if turn.tool_calls:
        stored["tool_calls"] = [{"id": call.id, "type": "function", "function": {
            "name": call.name, "arguments": json.dumps(call.arguments, ensure_ascii=False)}} for call in turn.tool_calls]
    return turn, [stored]
