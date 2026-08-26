"""Helpers for detecting JSON tool-call replies from web chat models."""

from __future__ import annotations

import json


def is_json_tool_reply(text: str) -> bool:
    try:
        value = json.loads(str(text or "").strip())
    except (TypeError, ValueError):
        return False
    return isinstance(value, dict) and bool(value.get("tool") or value.get("name"))


def looks_like_json_tool_reply(text: str) -> bool:
    clean = str(text or "").lstrip()
    return clean.startswith("{") and ('"tool"' in clean or '"name"' in clean)


def normalize_final_json_tool_reply(text: str) -> str:
    repaired = repair_missing_trailing_braces_json_tool_reply(text)
    return repaired or text


def repair_missing_trailing_braces_json_tool_reply(text: str) -> str:
    clean = str(text or "").strip()
    if not clean or not looks_like_json_tool_reply(clean):
        return ""
    if is_json_tool_reply(clean):
        return clean

    in_string = False
    escaped = False
    depth = 0
    started = False
    for char in clean:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == "{":
            started = True
            depth += 1
            continue
        if char == "}":
            if depth <= 0:
                return ""
            depth -= 1
            continue
        if started and depth == 0 and char.strip():
            return ""

    if in_string or depth <= 0 or depth > 2:
        return ""
    candidate = clean + ("}" * depth)
    return candidate if is_json_tool_reply(candidate) else ""
