"""Validate stored response bodies and derive conversation projections.

This format layer has no provider client or execution dependencies.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal, TypeAlias, TypedDict

from codey.durable.model import ArtifactRecord
from codey.types.json_value import JsonObject, decode_json_object, require_json_string


class ProviderCallPayload(TypedDict):
    id: str
    name: str
    arguments: JsonObject


class TextReplyPayload(TypedDict):
    protocol: Literal["text"]
    text: str


class NativeReplyPayload(TypedDict):
    protocol: Literal["native"]
    text: str
    reasoning: str
    finish: str
    raw: JsonObject
    tool_calls: list[ProviderCallPayload]


ProviderReplyPayload: TypeAlias = TextReplyPayload | NativeReplyPayload


def validate_response(payload: object) -> ProviderReplyPayload:
    payload = decode_json_object(payload)
    text = require_json_string(payload.get("text"), "provider response text")
    if payload.get("protocol") == "text" and set(payload) == {"protocol", "text"}:
        return {"protocol": "text", "text": text}
    if payload.get("protocol") != "native" or set(payload) != {
        "protocol", "text", "reasoning", "finish", "raw", "tool_calls",
    }:
        raise ValueError("provider response protocol is invalid")
    calls = payload["tool_calls"]
    if not isinstance(calls, list):
        raise ValueError("provider native response is invalid")
    parsed: list[ProviderCallPayload] = []
    for call in calls:
        if not isinstance(call, dict) or set(call) != {"id", "name", "arguments"}:
            raise ValueError("provider response call is invalid")
        parsed.append({"id": require_json_string(call["id"], "call id"),
                       "name": require_json_string(call["name"], "call name"),
                       "arguments": decode_json_object(call["arguments"])})
    finish = require_json_string(payload["finish"], "finish")
    if finish not in {"complete", "output_limit", "protocol_error"}:
        raise ValueError("provider response finish is invalid")
    return {"protocol": "native", "text": text,
            "reasoning": require_json_string(payload["reasoning"], "reasoning"),
            "finish": finish, "raw": decode_json_object(payload["raw"]), "tool_calls": parsed}


def tool_call_message(payload: object) -> dict[str, object]:
    reply = validate_response(payload)
    if reply["protocol"] != "native" or not reply["tool_calls"]:
        raise ValueError("provider history reference has no tool calls")
    message: dict[str, object] = {"content": reply["text"], "tool_calls": [
        {"id": call["id"], "type": "function", "function": {
            "name": call["name"], "arguments": json.dumps(call["arguments"], ensure_ascii=False),
        }} for call in reply["tool_calls"]
    ]}
    if reply["reasoning"].strip():
        message["reasoning_content"] = reply["reasoning"]
    return message


def read_response_artifact(record: ArtifactRecord) -> object:
    path = Path(record.storage_path)
    if path.stat().st_size != record.byte_count:
        raise ValueError("registered response artifact digest mismatch")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != record.sha256:
        raise ValueError("registered response artifact digest mismatch")
    return json.loads(raw.decode("utf-8"))
