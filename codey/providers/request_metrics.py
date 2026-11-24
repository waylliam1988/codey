"""Content-free measurements of outbound provider request sections."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from itertools import zip_longest

from codey.providers.task_prompt import TASK_HEADING
from codey.types.json_value import JsonObject, JsonValue, parse_json_object


@dataclass(frozen=True)
class RequestSection:
    name: str
    characters: int
    bytes: int
    digest: str


def text_section(name: str, text: str) -> RequestSection:
    data = text.encode("utf-8")
    return RequestSection(name, len(text), len(data), hashlib.sha256(data).hexdigest())


def _json_section(name: str, value: JsonValue) -> RequestSection:
    return text_section(name, json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def _item_section(item: JsonValue) -> RequestSection:
    if isinstance(item, dict):
        role = item.get("role")
        content = item.get("content")
        if isinstance(content, str):
            if content.startswith(TASK_HEADING):
                return text_section("task", content)
            if content.startswith("<request_context>"):
                return text_section("request_context", content)
            if content.startswith("Current work observations (runtime records):"):
                return text_section("observations", content)
            if content.startswith("[result:"):
                return text_section("tool_result", content)
            if isinstance(role, str):
                return text_section(role, content)
        if role == "tool" or item.get("type") == "function_call_output":
            return _json_section("tool_result", item)
    return _json_section("history_item", item)


def request_shape(raw: bytes) -> tuple[tuple[RequestSection, ...], JsonObject]:
    payload = parse_json_object(raw.decode("utf-8"))
    sections: list[RequestSection] = []
    for name, value in payload.items():
        if name in {"messages", "input"} and isinstance(value, list):
            sections.extend(_item_section(item) for item in value)
        elif name in {"tools", "response_format", "text"}:
            sections.append(_json_section(name, value))
    settings = {name: payload[name] for name in (
        "max_tokens", "max_output_tokens", "reasoning", "reasoning_effort", "chat_template_kwargs",
    ) if name in payload}
    return tuple(sections), settings


def first_changed_section(previous: tuple[RequestSection, ...], current: tuple[RequestSection, ...]) -> str | None:
    for old, new in zip_longest(previous, current):
        if old != new:
            return new.name if new is not None else old.name if old is not None else None
    return None
