"""Task-owned change projection from the original first-write baselines."""
from __future__ import annotations

from pathlib import Path

from codey.workspace.changes import ChangeTracker


def collect_task_changes(project: str | Path, tracker: ChangeTracker | None) -> dict[str, object]:
    root = Path(project).expanduser().resolve()
    if tracker is None:
        return {"ok": True, "mode": "snapshot", "root": str(root), "files": [],
                "changed_count": 0, "diff": "", "truncated": False}
    if tracker.root != root:
        raise ValueError("task change tracker belongs to another workspace")
    return tracker.collect_produced()
