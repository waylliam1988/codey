"""Lossless provider response encoding shared by receipts and history projections."""
from __future__ import annotations

from typing import cast

from codey.durable.provider_response import validate_response
from codey.providers.base import AssistantTurn, ProviderToolCall, TurnFinish
from codey.types.json_value import JsonValue, encode_transport_value


def json_value(value: object) -> JsonValue:
    """Freeze transport data without lossy default=str or callable encoding."""
    return encode_transport_value(value)


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
