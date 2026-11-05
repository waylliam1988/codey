"""Structured reasoning delta assembly within the shared SSE framing loop."""
from __future__ import annotations

from typing import Any

from codey.providers.api_stream import DefaultChatAccumulator
from codey.providers.cline.codec import DETAIL_KEYS, validate_details


class ClineAccumulator(DefaultChatAccumulator):
    def __init__(self) -> None:
        super().__init__()
        self.details: dict[int, dict[str, Any]] = {}

    def add_delta(self, delta: dict[str, Any]) -> None:
        super().add_delta(delta)
        fragments = delta.get("reasoning_details", [])
        if not isinstance(fragments, list):
            raise ValueError("invalid reasoning delta")
        for fragment in fragments:
            self._merge(fragment)
        if len(str(self.details)) > 1024 * 1024:
            raise ValueError("reasoning delta exceeded its limit")

    def _merge(self, fragment: object) -> None:
        if not isinstance(fragment, dict) or set(fragment) - DETAIL_KEYS:
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
                slot[key] = slot.get(key, "") + value

    def finish_message(self) -> dict[str, Any]:
        result = super().finish_message()
        if self.details:
            result["reasoning_details"] = validate_details([self.details[key] for key in sorted(self.details)])
        return result
