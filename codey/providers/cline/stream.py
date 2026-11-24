"""Structured reasoning delta assembly within the shared SSE framing loop."""
from __future__ import annotations

from codey.providers.api_stream import DefaultChatAccumulator
from codey.providers.cline.codec import DETAIL_KEYS, validate_details
from codey.types.json_value import JsonObject, JsonValue, decode_json_object, require_json_string


class ClineAccumulator(DefaultChatAccumulator):
    def __init__(self) -> None:
        super().__init__()
        self.details: dict[int, JsonObject] = {}

    def add_delta(self, delta: JsonObject) -> None:
        super().add_delta(delta)
        fragments = delta.get("reasoning_details", [])
        if not isinstance(fragments, list):
            raise ValueError("invalid reasoning delta")
        for fragment in fragments:
            self._merge(fragment)
        if len(str(self.details)) > 1024 * 1024:
            raise ValueError("reasoning delta exceeded its limit")

    def _merge(self, fragment: object) -> None:
        fragment = decode_json_object(fragment)
        if set(fragment) - DETAIL_KEYS:
            raise ValueError("unknown reasoning delta fields")
        index = fragment.get("index")
        identity = fragment.get("id")
        if index is None and isinstance(identity, str) and identity:
            found = [key for key, value in self.details.items() if value.get("id") == identity]
            index = found[0] if found else max(self.details, default=-1) + 1
        if index is None and len(self.details) == 1:
            index = next(iter(self.details))
        if type(index) is not int or not 0 <= index < 128:
            raise ValueError("ambiguous reasoning delta identity")
        slot = self.details.setdefault(index, {"index": index})
        for key, value in fragment.items():
            if key == "index":
                continue
            if not isinstance(value, str):
                raise ValueError("non-text reasoning delta")
            if key in {"type", "id", "format"}:
                if key in slot and slot[key] != value:
                    raise ValueError("reasoning delta changed identity or format")
                if key == "id" and any(other != index and detail.get("id") == value for other, detail in self.details.items()):
                    raise ValueError("reasoning id reused at another index")
                slot[key] = value
            else:
                slot[key] = require_json_string(slot.get(key, ""), key) + value

    def finish_message(self) -> JsonObject:
        result = super().finish_message()
        if self.details:
            details = validate_details([self.details[key] for key in sorted(self.details)])
            result["reasoning_details"] = list[JsonValue](decode_json_object(detail) for detail in details)
        return result
