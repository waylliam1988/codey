"""Line-number prefix stripping for edit failure-path retry.

Only used when an exact edit already failed. The stripped search must match
exactly once, otherwise the edit stays atomic-failed. Never writes line
numbers into the file.
"""

from __future__ import annotations

import re

_LINE_PREFIX_RE = re.compile(r"^\s*\d+\s*(?:\u2192|\||:)\s?")


def strip_line_number_prefixes(text: str) -> tuple[str, bool]:
    changed = False
    out: list[str] = []
    for line in str(text or "").splitlines(keepends=False):
        stripped = _LINE_PREFIX_RE.sub("", line, count=1)
        if stripped != line:
            changed = True
        out.append(stripped)
    return "\n".join(out), changed


__all__ = ["strip_line_number_prefixes"]
