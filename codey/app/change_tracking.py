"""Change-tracker resources owned by the existing AppContext."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from codey.workspace.changes import ChangeTracker, SnapshotStore


def change_tracker_for(state: Any, project: str | Path, *, persistent: bool,
                       task_id: str, limit: int) -> ChangeTracker:
    root = Path(project).expanduser().resolve()
    if not persistent:
        return ChangeTracker(root)
    if not task_id:
        # Recovery UI selects a task from original admission records, never
        # an independently maintained "latest task" pointer.
        for checkpoint in reversed(state.durable.tasks()):
            project = checkpoint.state.get("project")
            if not project or Path(project).expanduser().resolve() != root:
                continue
            scope = checkpoint.state.get("change_scope")
            if scope is None:
                continue
            if not isinstance(scope, str) or not scope:
                raise ValueError("invalid task change baseline scope")
            candidate = SnapshotStore(state.snapshot_store.state_home, task_id=scope)
            if candidate.path_for(root).exists():
                task_id = scope
                break
    key = str(root) if not task_id else f"{root}\0{task_id}"
    with state.lock:
        tracker: ChangeTracker | None = state.change_trackers.get(key)
        if tracker is None or tracker.store is None:
            tracker = ChangeTracker(root, SnapshotStore(state.snapshot_store.state_home, task_id=task_id))
            state.change_trackers[key] = tracker
        else:
            state.change_trackers.move_to_end(key)
        while len(state.change_trackers) > limit:
            _, old = state.change_trackers.popitem(last=False)
            old.disable_persistence()
        return tracker
