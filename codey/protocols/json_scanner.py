"""Shared balanced-brace scanner for JSON object extraction.

Pure character scan with string/escape handling. Callers own think-block
stripping, strictness, and acceptance rules so coding and research codecs
keep their own protocol semantics.
"""

from __future__ import annotations

import json
from typing import Any


def balanced_json_spans(text: str) -> list[tuple[int, int]]:
    """Return (start, end_exclusive) spans of top-level {...} objects."""
    spans: list[tuple[int, int]] = []
    in_string = False
    escaped = False
    depth = 0
    start: int | None = None
    for index, char in enumerate(text):
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
            if depth == 0:
                start = index
            depth += 1
            continue
        if char == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                spans.append((start, index + 1))
                start = None
    return spans


def extract_json_objects(text: str) -> list[dict[str, Any]]:
    """Decode balanced top-level JSON objects from a model reply."""
    objects: list[dict[str, Any]] = []
    for start, end in balanced_json_spans(str(text or "")):
        try:
            value = json.loads(text[start:end], strict=False)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            objects.append(value)
    return objects


__all__ = ["balanced_json_spans", "extract_json_objects"]
