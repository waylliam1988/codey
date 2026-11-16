"""Mask task data without changing the offsets of executable instructions."""
from __future__ import annotations

import re

_DATA = re.compile(
    r"```[\s\S]*?```|~~~[\s\S]*?~~~|(?m:^[ \t]*>[^\n]*)|(?P<ticks>`+)(?!`)[^\r\n]*?(?P=ticks)(?!`)|"
    r'"(?:\\.|[^"\\\n])*"|'
    r"(?<![A-Za-z0-9_])'(?:\\.|(?<=[A-Za-z0-9_])'(?=[A-Za-z0-9_])|[^'\\\n])*'(?![A-Za-z0-9_])|"
    r"“[^”]*”|‘(?:(?<=[A-Za-z0-9_])’(?=[A-Za-z0-9_])|[^’])*’|「[^」]*」|『[^』]*』"
)
_INLINE_COMMAND = re.compile(r"(?:^|[，,。；;\n]|调用|使用|执行|再|先|最后|\bcall|\buse|\bthen)\s*$", re.I)


def quoted_value(text: str) -> str | None:
    """Read raw quoted data; unmatched delimiters yield empty, unquoted text yields None."""
    if not text.startswith(('"', "'", "“", "‘", "「", "『", "`")):
        return None
    match = _DATA.match(text)
    if match is None:
        return ""
    value = match[0]
    width = len(value) - len(value.lstrip("`")) if value.startswith("`") else 1
    return value[width:-width]


def instruction_text(task: str, *, inline_names: frozenset[str] = frozenset()) -> str:
    """Keep explicitly called bare tool names; mask quoted labels and examples."""
    def mask(match: re.Match[str]) -> str:
        value = match[0]
        name = value.strip("`")
        if (value.startswith("`") and name in inline_names
                and _INLINE_COMMAND.search(task[:match.start()]) is not None):
            padding = " " * ((len(value) - len(name)) // 2)
            return padding + name + padding
        return "".join(char if char in "\r\n" else " " for char in value)

    return _DATA.sub(mask, task)
