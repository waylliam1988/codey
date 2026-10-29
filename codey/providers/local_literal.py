"""Deterministic grammar for local tool-frame argument objects.

Bare identifier keys, quoted strings (including Gemma quote delimiters), JSON
containers and explicit boolean/null tokens are supported. No repair passes
operate on the input, especially not on string contents.
"""

from __future__ import annotations

import json
import re
from typing import cast

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
_MARKERS = ('<|"|>', '<|\\"|>')
_LITERALS = {"true": True, "false": False, "null": None, "True": True, "False": False, "None": None}


class _Parser:
    def __init__(self, source: str) -> None:
        self.source = source
        self.pos = 0

    def whitespace(self) -> None:
        while self.pos < len(self.source) and self.source[self.pos].isspace():
            self.pos += 1

    def take(self, token: str) -> bool:
        self.whitespace()
        if self.source.startswith(token, self.pos):
            self.pos += len(token)
            return True
        return False

    def string(self) -> str:
        self.whitespace()
        start = self.pos
        delimiter = next((m for m in _MARKERS if self.source.startswith(m, start)), None)
        if delimiter is not None:
            self.pos += len(delimiter)
            encoded: list[str] = []
            while self.pos < len(self.source):
                if self.source.startswith(delimiter, self.pos):
                    self.pos += len(delimiter)
                    # Marker strings use JSON escapes, with ordinary quotes
                    # as data rather than as delimiters. Unlike JSON quotes,
                    # marker delimiters also permit literal control characters.
                    value = json.loads('"' + "".join(encoded) + '"')
                    if type(value) is not str:
                        raise ValueError("expected a string")
                    return value
                char = self.source[self.pos]
                if char == "\\":
                    encoded.append(self.source[self.pos : self.pos + 2])
                    self.pos += 1
                else:
                    encoded.append(json.dumps(char)[1:-1] if ord(char) < 32 else '\\"' if char == '"' else char)
                self.pos += 1
        elif self.source[start : start + 1] in ("'", '"'):
            quote = self.source[start]
            encoded = []
            self.pos += 1
            while self.pos < len(self.source):
                char = self.source[self.pos]
                self.pos += 1
                if char == "\\":
                    escaped = self.source[self.pos : self.pos + 1]
                    encoded.append("'" if escaped == "'" and quote == "'" else "\\" + escaped)
                    self.pos += 1
                elif char == quote:
                    value = json.loads('"' + "".join(encoded) + '"')
                    if type(value) is not str:
                        raise ValueError("expected a string")
                    return value
                else:
                    encoded.append('\\"' if char == '"' else char)
        raise ValueError("unterminated string")

    def value(self) -> object:
        self.whitespace()
        if self.take("{"):
            result: dict[str, object] = {}
            if self.take("}"):
                return result
            while True:
                self.whitespace()
                identifier = _IDENTIFIER.match(self.source, self.pos)
                if identifier:
                    key = identifier.group()
                    self.pos = identifier.end()
                else:
                    key = self.string()
                if key in result or not self.take(":"):
                    raise ValueError("duplicate key or missing colon")
                result[key] = self.value()
                if self.take("}"):
                    return result
                if not self.take(","):
                    raise ValueError("expected object separator")
        if self.take("["):
            items: list[object] = []
            if self.take("]"):
                return items
            while True:
                items.append(self.value())
                if self.take("]"):
                    return items
                if not self.take(","):
                    raise ValueError("expected array separator")
        identifier = _IDENTIFIER.match(self.source, self.pos)
        if identifier and identifier.group() in _LITERALS:
            self.pos = identifier.end()
            return _LITERALS[identifier.group()]
        number = _NUMBER.match(self.source, self.pos)
        if number:
            self.pos = number.end()
            return json.loads(number.group())
        return self.string()


def parse_mapping(source: str) -> dict[str, object] | None:
    try:
        parser = _Parser(source)
        value = parser.value()
        parser.whitespace()
        if parser.pos != len(source) or type(value) is not dict:
            return None
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        return cast(dict[str, object], value)
    except (SyntaxError, ValueError, TypeError, MemoryError, RecursionError):
        return None
