"""Cline's bounded Chat extension; common tool pairing remains authoritative."""
from __future__ import annotations

import copy
import json
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from codey.providers import api_chat
from codey.providers.api_codec import GenerationSettings
from codey.providers.base import AssistantTurn, ProviderToolDefinition

CODEC_REVISION = "cline-chat-reasoning-v1"
DETAIL_TYPES = frozenset({"reasoning.text", "reasoning.summary", "reasoning.encrypted"})
DETAIL_KEYS = frozenset({"type", "id", "index", "format", "text", "summary", "data", "signature"})


def unwrap(body: dict[str, Any]) -> dict[str, Any]:
    data = body.get("data")
    if body.get("success") is True and isinstance(data, dict):
        return data
    return body


def validate_details(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > 128 or len(json.dumps(value)) > 1024 * 1024:
        raise ValueError("unsupported reasoning metadata size or structure")
    for item in value:
        if not isinstance(item, dict) or item.get("type") not in DETAIL_TYPES or set(item) - DETAIL_KEYS:
            raise ValueError("unsupported reasoning metadata contract")
        for key, field in item.items():
            if (key == "index" and (type(field) is not int or field < 0)) or (key != "index" and not isinstance(field, str)):
                raise ValueError("unsupported reasoning metadata field")
    return copy.deepcopy(value)


class ChatCodec:
    endpoint = api_chat.endpoint
    prepare = staticmethod(api_chat.prepare)
    pending_calls = staticmethod(api_chat.pending_calls)
    closed_spans = staticmethod(api_chat.closed_spans)
    checkpoint_item = staticmethod(api_chat.checkpoint_item)
    encode_results = staticmethod(api_chat.encode_results)

    @staticmethod
    def validate_view(items: list[dict[str, Any]]) -> None:
        api_chat.validate_view(items)
        for item in items:
            if item.get("role") == "assistant" and item.get("reasoning_details") is not None:
                validate_details(item["reasoning_details"])

    @staticmethod
    def build_payload(items: list[dict[str, Any]], tools: list[ProviderToolDefinition] | None,
                      settings: GenerationSettings) -> dict[str, Any]:
        return api_chat.build_payload(items, tools, settings)

    @staticmethod
    def encode_context(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # Semantic replay/model switches never import another window's opaque data.
        result = api_chat.encode_context(messages)
        for message in result:
            message.pop("reasoning_details", None)
        return result

    @staticmethod
    def decode_exchange(body: dict[str, Any], *, text_only: bool,
                        text_decoder: Callable[[str], str | AssistantTurn] | None,
                        ) -> tuple[AssistantTurn, list[dict[str, Any]] | None]:
        body = unwrap(body)
        choices = body.get("choices")
        if not isinstance(choices, list) or len(choices) != 1:
            raise ValueError("Cline requires exactly one completion choice")
        message = choices[0].get("message") or {}
        details: list[dict[str, Any]] = validate_details(message["reasoning_details"]) if message.get("reasoning_details") is not None else []
        turn, stored = api_chat.decode_exchange(body, text_only=text_only, text_decoder=text_decoder)
        if details and stored:
            stored[-1]["reasoning_details"] = details
            if not turn.reasoning:
                text = "\n".join(item.get("text") or item.get("summary") or "" for item in details)
                turn = replace(turn, reasoning=text.strip())
        return turn, stored
