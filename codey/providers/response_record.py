"""Lossless provider response encoding shared by receipts and history projections."""
from __future__ import annotations

from codey.durable.provider_response import validate_response
from codey.providers.base import AssistantTurn, ProviderToolCall, TurnFinish
from codey.types.json_value import (
    JsonObject,
    JsonValue,
    decode_json_object,
    encode_transport_value,
    require_json_string,
)


def json_value(value: object) -> JsonValue:
    """Freeze transport data without lossy default=str or callable encoding."""
    return encode_transport_value(value)


def _finish_value(value: object) -> str:
    if not isinstance(value, TurnFinish):
        raise ValueError("provider returned an invalid finish")
    return value.value


def _call_payload(value: object) -> JsonObject:
    if not isinstance(value, ProviderToolCall):
        raise ValueError("provider returned an invalid tool call")
    return {"id": require_json_string(value.id, "call id"), "name": require_json_string(value.name, "call name"),
            "arguments": decode_json_object(value.arguments)}


def encode_reply(reply: object) -> dict[str, object]:
    if isinstance(reply, str):
        return {"protocol": "text", "text": reply}
    if not isinstance(reply, AssistantTurn):
        raise ValueError("provider returned an unsupported response type")
    return {"protocol": "native", "text": require_json_string(reply.text, "text"),
            "reasoning": require_json_string(reply.reasoning, "reasoning"),
            "finish": _finish_value(reply.finish), "raw": decode_json_object(reply.raw),
            "tool_calls": [_call_payload(call) for call in reply.tool_calls]}


def decode_reply(payload: object) -> str | AssistantTurn:
    payload = validate_response(payload)
    if payload["protocol"] == "text":
        return payload["text"]
    calls = payload["tool_calls"]
    parsed = []
    for call in calls:
        # Invalid/missing ids remain raw protocol errors; this codec does not repair them.
        parsed.append(ProviderToolCall(call["id"], call["name"], call["arguments"]))
    return AssistantTurn(text=payload["text"], tool_calls=tuple(parsed), raw=payload["raw"],
                         reasoning=payload["reasoning"], finish=TurnFinish(payload["finish"]))
