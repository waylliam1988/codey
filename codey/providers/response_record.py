"""Lossless provider response encoding shared by receipts and history projections."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from enum import Enum
from typing import cast

from codey.durable.provider_response import validate_response
from codey.providers.base import AssistantTurn, ProviderToolCall, TurnFinish


def json_value(value: object) -> object:
    """Freeze transport data without lossy default=str or callable encoding."""
    if value is None or type(value) in (str, bool, int, float):
        return value
    if isinstance(value, Enum):
        return json_value(value.value)
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("provider receipt object keys must be strings")
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        values = sorted(value) if isinstance(value, (set, frozenset)) else value
        return [json_value(item) for item in values]
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: json_value(getattr(value, field.name)) for field in fields(value)}
    raise ValueError(f"provider receipt cannot encode {type(value).__name__}")


def encode_reply(reply: object) -> dict[str, object]:
    if isinstance(reply, str):
        return {"protocol": "text", "text": reply}
    if not isinstance(reply, AssistantTurn):
        raise ValueError("provider returned an unsupported response type")
    return {"protocol": "native", "text": reply.text, "reasoning": reply.reasoning,
            "finish": reply.finish.value, "raw": json_value(reply.raw),
            "tool_calls": [{"id": call.id, "name": call.name, "arguments": json_value(call.arguments)}
                           for call in reply.tool_calls]}


def decode_reply(payload: object) -> str | AssistantTurn:
    payload = validate_response(payload)
    if payload["protocol"] == "text":
        return cast(str, payload["text"])
    calls = payload["tool_calls"]
    parsed = []
    for call in calls:
        # Invalid/missing ids remain raw protocol errors; this codec does not repair them.
        parsed.append(ProviderToolCall(call["id"], call["name"], call["arguments"]))
    return AssistantTurn(text=payload["text"], tool_calls=tuple(parsed), raw=payload["raw"],
                         reasoning=payload["reasoning"], finish=TurnFinish(payload["finish"]))
