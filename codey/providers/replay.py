"""Project portable durable history into an API connection's tool protocol."""
from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence

from codey.providers.error_classification import RequestPrepError
from codey.runtime.core.protocol_window import ProviderBinding
from codey.types.json_value import JsonObject, decode_json_object, require_json_string
from codey.types.tool_image import TOOL_IMAGES_KEY

_PORTABLE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_PRIVATE_STATE = frozenset({"reasoning", "reasoning_content", "signature", "thought_signature", "encrypted_content"})


def transform_replay_messages(messages: Sequence[Mapping[str, object]], target_binding: ProviderBinding, *,
                              supports_images: bool) -> list[tuple[int, JsonObject]]:
    """Keep each row tied to its original offset, including extra tool outputs."""
    consumed: set[str] = set()
    calls: dict[str, str] = {}
    answered: set[str] = set()
    replay: list[tuple[int, JsonObject]] = []
    extra: list[tuple[int, JsonObject]] = []
    for index, raw in enumerate(messages):
        row = decode_json_object(dict(raw))
        if not supports_images and row.pop(TOOL_IMAGES_KEY, None):
            row["content"] = str(row.get("content") or "") + "\nA prior tool result included an image. This connection can replay its text."
        if row.get("role") == "tool":
            original = require_json_string(row.get("tool_call_id"), "durable tool result call ID")
            if original not in calls:
                raise RequestPrepError("durable replay contains an unpaired tool result")
            row["tool_call_id"] = calls[original]
            if original in answered:
                supplement: JsonObject = {"role": "user",
                    "content": f"Additional observed output for tool call {calls[original]}:\n{row.get('content', '')}"}
                if TOOL_IMAGES_KEY in row:
                    supplement[TOOL_IMAGES_KEY] = row[TOOL_IMAGES_KEY]
                extra.append((index, supplement))
                continue
            answered.add(original)
        else:
            replay.extend(extra)
            extra.clear()
            calls.clear()
            answered.clear()
            if "tool_calls" in row:
                raw_calls = row["tool_calls"]
                if not isinstance(raw_calls, list):
                    raise RequestPrepError("durable replay contains invalid tool calls")
                portable: list[JsonObject] = []
                for position, raw_call in enumerate(raw_calls):
                    call = decode_json_object(raw_call)
                    original = require_json_string(call.get("id"), "durable tool call ID")
                    if original in calls:
                        raise RequestPrepError("durable replay contains ambiguous tool calls in one message")
                    identity = _replay_id(original, target_binding.protocol, index, position, consumed)
                    calls[original] = identity
                    consumed.add(identity)
                    portable.append({"id": identity, "type": "function", "function": call["function"]})
                row["tool_calls"] = list(portable)
        replay.append((index, {key: value for key, value in row.items() if key not in _PRIVATE_STATE}))
    replay.extend(extra)
    return replay


def _replay_id(original: str, protocol: str, index: int, position: int, consumed: set[str]) -> str:
    if _PORTABLE_ID.fullmatch(original) and original not in consumed:
        return original
    digest = hashlib.sha256(f"{protocol}:{index}:{position}:{original}".encode()).hexdigest()[:24]
    identity = "call_" + digest
    while identity in consumed:
        identity = "call_" + hashlib.sha256(identity.encode()).hexdigest()[:24]
    return identity
