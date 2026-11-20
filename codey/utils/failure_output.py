"""Bounded verbatim failure excerpts, never execution or completion evidence."""
from __future__ import annotations

import re

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_SUMMARY = re.compile(r"^(?:=+\s*)?\d+ (?:failed|error|errors)\b.*")


def failure_excerpt(output: str, *, limit: int = 1600) -> str:
    if limit <= 0:
        return ""
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
    if len(rendered) + len("\n".join(selected)) + 1 <= limit:
        return rendered + "\n".join(selected) + "\n"
    for index, line in enumerate(selected):
        omitted = f"[{len(selected) - index} further failure excerpt lines omitted.]\n"
        following = f"[{len(selected) - index - 1} further failure excerpt lines omitted.]\n"
        if len(rendered) + len(line) + 1 + len(following) > limit:
            return rendered + omitted if len(rendered) + len(omitted) <= limit else omitted[:limit]
        rendered += line + "\n"
    return rendered
