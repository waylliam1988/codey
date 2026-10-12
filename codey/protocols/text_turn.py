"""Decode the existing text tool protocol at the transport boundary.

This module only recognizes the outer text framing.  Authorization, argument
validation, batch lowering, execution and completion remain in the kernel.
The accepted shapes intentionally mirror the former kernel text decoder.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from codey.protocols.json_scanner import balanced_json_spans

MAX_TEXT_REQUESTS = 8


@dataclass(frozen=True)
class TextRequest:
    tool: str
    args: dict[str, Any]


@dataclass(frozen=True)
class TextRequests:
    items: tuple[TextRequest, ...]


@dataclass(frozen=True)
class TextAnswer:
    text: str


@dataclass(frozen=True)
class TextError:
    message: str
    kind: str = "invalid_args"
    tool: str = ""


DecodedTextTurn = TextRequests | TextAnswer | TextError


def _tool_and_args(obj: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    tool = str(obj.get("tool", "") or "").strip().lower()
    raw_args = obj.get("args", None)
    args: dict[str, Any]
    if isinstance(raw_args, dict):
        args = dict(raw_args)
    elif raw_args is None:
        args = {key: value for key, value in obj.items() if key != "tool"}
    else:
        args = {}
    return tool, args


def _json_objects(text: str) -> list[dict[str, Any]] | TextError | None:
    source = str(text or "").strip()
    if source.startswith("```"):
        return None
    spans = balanced_json_spans(source)
    if not spans:
        return None
    first_start = spans[0][0]
    last_end = spans[-1][1]
    if source[:first_start].strip() or source[last_end:].strip():
        return None
    if any(source[end:start].strip() for (_, end), (start, _) in zip(spans, spans[1:], strict=False)):
        return None
    objects: list[dict[str, Any]] = []
    for start, end in spans:
        try:
            value = json.loads(source[start:end])
        except json.JSONDecodeError as exc:
            detail = f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}."
            if exc.msg.startswith("Invalid control character"):
                detail += (
                    r" Escape line breaks and tabs inside JSON strings as \n, \r and \t;"
                    r' for example, "content":"first line\nsecond line\n".'
                )
            return TextError(detail, kind="invalid_json")
        except (ValueError, RecursionError):
            return TextError("invalid JSON: value exceeds decoder limits", kind="invalid_json")
        if isinstance(value, dict):
            objects.append(value)
    return objects


def decode_text_turn(text: str) -> DecodedTextTurn:
    """Decode one complete text reply without consulting task state."""
    source = str(text or "").strip()
    objects = _json_objects(source)
    if isinstance(objects, TextError):
        return objects
    if objects:
        if all("tool" not in obj for obj in objects):
            if len(objects) > 1:
                return TextError("incomplete or malformed JSON response", kind="invalid_json")
            return TextAnswer(source)
        if len(objects) > MAX_TEXT_REQUESTS:
            return TextError(
                f"too many native tool calls in one turn ({len(objects)}); send at most {MAX_TEXT_REQUESTS}",
                kind="too_many_tools",
            )
        return TextRequests(tuple(TextRequest(*_tool_and_args(obj)) for obj in objects))

    if not source:
        return TextError("no JSON tool call found", kind="no_json")
    if source.startswith("<|tool_call>"):
        return TextError("no JSON tool call found", kind="no_json")
    if source.startswith(("{", "[")):
        try:
            json.loads(source)
        except (ValueError, RecursionError):
            return TextError("incomplete or malformed JSON response", kind="invalid_json")
    return TextAnswer(source)


__all__ = [
    "DecodedTextTurn",
    "MAX_TEXT_REQUESTS",
    "TextAnswer",
    "TextError",
    "TextRequest",
    "TextRequests",
    "decode_text_turn",
]
