"""Recover unique Python edits with a uniform margin or safe fragment boundary."""
from __future__ import annotations

import ast
import io
import tokenize

from codey.toolchain.edit_text import TextEdit, count_matches


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


def align_replacement(content: str, search: str, replacement: str) -> TextEdit | None:
    """Prepare edit coordinates; the caller owns version checks and writing.

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
    return TextEdit(start, end, shifted) if valid_python(updated) else None


def boundary_replacement(content: str, search: str, replacement: str) -> TextEdit | None:
    """Recover symmetric fragment padding, or one extra newline at actual EOF.

    Python tokenization excludes multiline literals. The old fragment still has
    to match uniquely at a line boundary, with valid Python before and after.
    """
    padding = search.rsplit("\n", 1)[-1]
    eof_only = False
    if "\n" in search and padding and not padding.strip(" ") and replacement.endswith("\n" + padding):
        old, new = search[:-len(padding)], replacement[:-len(padding)]
    elif search.endswith("\n") and replacement.endswith("\n"):
        old, new = search[:-1], replacement[:-1]
        eof_only = True
    else:
        return None
    if not old.strip() or count_matches(content, old) != 1:
        return None
    start = content.find(old)
    end = start + len(old)
    if ((start and content[start - 1] != "\n") or (eof_only and end != len(content))
            or not valid_python(content)):
        return None
    protected = _literal_lines(content)
    first_line = content.count("\n", 0, start) + 1
    last_line = content.count("\n", 0, end) + 1
    if (protected is None or protected.intersection(range(first_line, last_line + 1))
            or _literal_lines(replacement) != set()):
        return None
    edit = TextEdit(start, end, new)
    return edit if valid_python(content[:start] + new + content[end:]) else None
