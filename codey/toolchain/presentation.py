"""Bounded evidence for display, computed from actual file contents."""
from __future__ import annotations

from difflib import unified_diff


def edit_preview(path: str, before: str | None, after: str) -> dict[str, object]:
    if before is None or len(before) + len(after) > 128_000:
        return {"diff_unavailable": True}
    lines = list(unified_diff(before.splitlines(), after.splitlines(), fromfile=path, tofile=path, n=3, lineterm=""))
    diff = "\n".join(lines)
    return {
        "additions": sum(line.startswith("+") and not line.startswith("+++") for line in lines),
        "deletions": sum(line.startswith("-") and not line.startswith("---") for line in lines),
        "diff": diff[:16_000], "diff_truncated": len(diff) > 16_000,
    }
