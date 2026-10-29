"""Validate stored response bodies and derive conversation projections.

This format layer has no provider client or execution dependencies.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from codey.durable.model import ArtifactRecord


def validate_response(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
        raise ValueError("provider response payload is invalid")
    if payload.get("protocol") == "text" and set(payload) == {"protocol", "text"}:
        return payload
    if payload.get("protocol") != "native" or set(payload) != {
        "protocol", "text", "reasoning", "finish", "raw", "tool_calls",
    }:
        raise ValueError("provider response protocol is invalid")
    calls = payload["tool_calls"]
    if (not isinstance(calls, list) or not isinstance(payload["raw"], dict)
            or not isinstance(payload["reasoning"], str)):
        raise ValueError("provider native response is invalid")
    for call in calls:
        if (not isinstance(call, dict) or set(call) != {"id", "name", "arguments"}
                or not isinstance(call["id"], str) or not isinstance(call["name"], str)
                or not isinstance(call["arguments"], dict)):
            raise ValueError("provider response call is invalid")
    if payload["finish"] not in {"complete", "output_limit", "protocol_error"}:
        raise ValueError("provider response finish is invalid")
    return payload


def tool_call_message(payload: object) -> dict[str, object]:
    reply = validate_response(payload)
    if reply["protocol"] != "native" or not reply["tool_calls"]:
        raise ValueError("provider history reference has no tool calls")
    return {"content": reply["text"], "tool_calls": [
        {"id": call["id"], "type": "function", "function": {
            "name": call["name"], "arguments": json.dumps(call["arguments"], ensure_ascii=False),
        }} for call in reply["tool_calls"]
    ]}


def read_response_artifact(record: ArtifactRecord) -> object:
    path = Path(record.storage_path)
    if path.stat().st_size != record.byte_count:
        raise ValueError("registered response artifact digest mismatch")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != record.sha256:
        raise ValueError("registered response artifact digest mismatch")
    return json.loads(raw.decode("utf-8"))
