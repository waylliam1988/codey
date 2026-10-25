"""Task-owned change projection from the original first-write baselines."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from codey.workspace.changes import ChangeTracker
from codey.workspace.paths import path_hash, safe_join


def collect_task_changes(project: str | Path, tracker: ChangeTracker | None) -> dict[str, Any]:
    root = Path(project).expanduser().resolve()
    if tracker is None:
        return {"ok": True, "mode": "snapshot", "root": str(root), "files": [],
                "changed_count": 0, "diff": "", "truncated": False}
    if tracker.root != root:
        raise ValueError("task change tracker belongs to another workspace")
    with tracker._lock:
        produced = dict(tracker._after_hashes)
    for rel, expected in produced.items():
        if path_hash(safe_join(root, rel)) != expected:
            return {"ok": False, "error": f"task output changed externally: {rel}",
                    "files": [], "changed_count": 0, "diff": ""}
    return tracker.collect(paths=list(produced))
