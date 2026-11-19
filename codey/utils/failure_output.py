"""Bounded verbatim failure excerpts, never execution or completion evidence."""
from __future__ import annotations

import re

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_SUMMARY = re.compile(r"^(?:=+\s*)?\d+ (?:failed|error|errors)\b.*")


def failure_excerpt(output: str, *, limit: int = 1600) -> str:
    lines = [_ANSI.sub("", line).strip() for line in output.splitlines()]
    summaries = [line.strip("= ") for line in lines if _SUMMARY.match(line)]
    failed = [line for line in lines if line.startswith(("FAILED ", "ERROR ", "ERROR: "))]
    details = [line for line in lines if line.startswith("E ")]
    selected: list[str] = []
    for line in [*summaries[-1:], *failed, *details[:3]]:
        if line not in selected:
            selected.append(line)
    if not selected:
        return ""
    rendered = "Failure excerpts (reported by command):\n"
    for line in selected:
        if len(rendered) + len(line) + 1 > limit:
            return (rendered + "[Further failure excerpts omitted.]\n")[:limit]
        rendered += line + "\n"
    return rendered
