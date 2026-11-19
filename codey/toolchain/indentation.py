"""Recover only a unique, uniform outer-space offset in an existing edit."""
from __future__ import annotations

import ast
import io
import tokenize


def _lines(text: str) -> list[str]:
    return io.StringIO(text).readlines()


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _literal_lines(text: str) -> set[int] | None:
    protected: set[int] = set()
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if ((token.type == tokenize.STRING or tokenize.tok_name[token.type].startswith("FSTRING"))
                    and token.start[0] != token.end[0]):
                protected.update(range(token.start[0], token.end[0] + 1))
    except (tokenize.TokenError, SyntaxError):
        return None
    return protected


def _spaces_only(lines: list[str]) -> bool:
    return all(not line.strip() or line.lstrip(" ") == line.lstrip() for line in lines)


def valid_python(text: str) -> bool:
    try:
        ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return False
    return True


def _offset(window: list[str], search: list[str]) -> int | None:
    delta = _indent(window[0]) - _indent(search[0])
    if not delta or not _spaces_only(window):
        return None
    for actual, expected in zip(window, search, strict=True):
        actual, expected = actual.rstrip("\n"), expected.rstrip("\n")
        if not expected.strip():
            if actual != expected:
                return None
        elif (actual.lstrip(" ") != expected.lstrip(" ")
              or _indent(actual) - _indent(expected) != delta):
            return None
    return delta


def align_replacement(content: str, search: str, replacement: str) -> str | None:
    """Prepare one file value; the caller still owns version checks and writing.

    Exact edits never enter here. Every physical line must match, including its
    relative indentation, and the replacement must retain the old block margin.
    """
    search = search.replace("\r\n", "\n")
    replacement = replacement.replace("\r\n", "\n")
    wanted, new = _lines(search), _lines(replacement)
    if (sum(bool(line.strip()) for line in wanted) < 2 or not wanted[0].strip()
            or not _spaces_only(wanted + new)):
        return None
    margin = min(_indent(line) for line in wanted if line.strip())
    if new and min((_indent(line) for line in new if line.strip()), default=margin) != margin:
        return None
    lines = _lines(content)
    matches: list[tuple[int, int]] = []
    for index in range(len(lines) - len(wanted) + 1):
        if lines[index].lstrip(" ") != wanted[0].lstrip(" "):
            continue
        delta = _offset(lines[index:index + len(wanted)], wanted)
        if delta is not None:
            matches.append((index, delta))
            if len(matches) > 1:
                return None
    if not matches or not valid_python(content):
        return None
    index, delta = matches[0]
    protected = _literal_lines(content)
    if (protected is None or protected.intersection(range(index + 1, index + len(wanted) + 1))
            or _literal_lines(replacement) != set()):
        return None
    shifted = "".join((" " * delta + line if delta > 0 else line[-delta:]) if line.strip() else line
                      for line in new)
    start = sum(map(len, lines[:index]))
    end = start + sum(map(len, lines[index:index + len(wanted)]))
    if not search.endswith("\n") and content[start:end].endswith("\n"):
        end -= 1
    updated = content[:start] + shifted + content[end:]
    return updated if valid_python(updated) else None
