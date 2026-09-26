"""Shared balanced-brace scanner for JSON object extraction.

Pure character scan with string/escape handling. Callers own think-block
stripping, strictness, and acceptance rules so coding and research codecs
keep their own protocol semantics.
"""

from __future__ import annotations


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


__all__ = ["balanced_json_spans"]
