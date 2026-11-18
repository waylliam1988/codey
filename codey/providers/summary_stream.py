"""Read only an ordinary done.summary string from incomplete JSON arguments.

This is a provisional display projection, not argument validation. Other fields,
including collaboration metadata, are never returned to the display stream.
"""
from __future__ import annotations

import json


def _string(text: str, start: int) -> tuple[str, int, bool]:
    index = start + 1
    boundary = index
    while index < len(text):
        char = text[index]
        if char == '"':
            value: object = json.loads(text[start:index + 1])
            return (value if isinstance(value, str) else ""), index + 1, True
        if char == "\\":
            if index + 1 >= len(text):
                break
            width = 6 if text[index + 1] == "u" else 2
            if index + width > len(text):
                break
            if width == 6:
                code = int(text[index + 2:index + 6], 16)
                if 0xD800 <= code <= 0xDBFF:
                    if index + 12 > len(text) or text[index + 6:index + 8] != "\\u":
                        break
                    low = int(text[index + 8:index + 12], 16)
                    if not 0xDC00 <= low <= 0xDFFF:
                        raise ValueError("unpaired summary surrogate")
                    width = 12
                elif 0xDC00 <= code <= 0xDFFF:
                    raise ValueError("unpaired summary surrogate")
            index += width
        else:
            if ord(char) < 0x20:
                raise ValueError("unescaped summary control character")
            index += 1
        boundary = index
    value = json.loads(text[start:boundary] + '"')
    return (value if isinstance(value, str) else ""), boundary, False


def summary_prefix(arguments: str) -> str:
    """Select a top-level summary, never matching keys inside another value."""
    text = arguments.lstrip()
    if not text.startswith("{"):
        return ""
    index = 1
    try:
        while index < len(text):
            while index < len(text) and text[index] in " \t\r\n,":
                index += 1
            if index >= len(text) or text[index] != '"':
                return ""
            key, index, complete = _string(text, index)
            if not complete:
                return ""
            while index < len(text) and text[index].isspace():
                index += 1
            if index >= len(text) or text[index] != ":":
                return ""
            index += 1
            while index < len(text) and text[index].isspace():
                index += 1
            if index >= len(text):
                return ""
            if key == "summary":
                return _string(text, index)[0] if text[index] == '"' else ""
            # Skip complete earlier values without interpreting their content.
            depth = 0
            while index < len(text):
                char = text[index]
                if char == '"':
                    _, index, complete = _string(text, index)
                    if not complete:
                        return ""
                    continue
                if char in "[{":
                    depth += 1
                elif char in "]}":
                    if depth == 0:
                        return ""
                    depth -= 1
                elif char == "," and depth == 0:
                    index += 1
                    break
                index += 1
        return ""
    except (ValueError, UnicodeError):
        return ""
