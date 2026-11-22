"""Shared balanced-brace scanner for JSON object extraction.

Pure character scan with string/escape handling. Callers own think-block
stripping, strictness, and acceptance rules so coding and research codecs
keep their own protocol semantics.
"""

from __future__ import annotations

import json

from codey.utils.object_fields import object_fields


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


def _extract_objects(text: str, *, strict: bool) -> list[dict[str, object]]:
    objects: list[dict[str, object]] = []
    for start, end in balanced_json_spans(text or ""):
        try:
            value: object = json.loads(text[start:end], strict=strict)
        except json.JSONDecodeError:
            continue
        if object_fields(value):
            objects.append(value)
    return objects


def extract_json_objects(text: str) -> list[dict[str, object]]:
    """Decode balanced top-level JSON objects from a model reply."""
    return _extract_objects(text, strict=False)


def extract_candidate_json_objects(text: str) -> list[dict[str, object]]:
    """Extract balanced candidates after removing provider think blocks.

    This preserves the narrow repair-prompt lookup behavior that used to live
    beside ``JsonToolCodec``.  It is not an execution decoder and does not
    authorize or execute any returned object.
    """
    source = (text or "")
    lowered = source.lower()
    cleaned: list[str] = []
    index = 0
    depth = 0
    in_string = False
    escaped = False
    while index < len(source):
        if depth == 0 and lowered.startswith("<think", index):
            tag_end = lowered.find(">", index)
            if tag_end < 0:
                break
            end = lowered.find("</think>", tag_end + 1)
            if end < 0:
                break
            index = end + len("</think>")
            continue
        char = source[index]
        cleaned.append(char)
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"' and depth:
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}" and depth:
            depth -= 1
        index += 1
    cleaned_text = "".join(cleaned)
    return _extract_objects(cleaned_text, strict=True)


__all__ = ["balanced_json_spans", "extract_candidate_json_objects", "extract_json_objects"]
