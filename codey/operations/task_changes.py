"""Bind task-scoped baselines through the original durable task checkpoint."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from codey.durable.model import TaskCheckpoint


def task_change_tracker(state: Any, project: str | Path, task_id: str, *, source_id: str = "") -> Any:
    root = Path(project).expanduser().resolve()

    def bind(current: TaskCheckpoint) -> dict[str, object] | None:
        selected = current.state.get("change_scope")
        if selected is not None:
            if not isinstance(selected, str) or not selected:
                raise ValueError("invalid task change scope")
            return None
        scope = source_id or str(current.state.get("previous_run_id") or "") or task_id
        if scope != task_id:
            source = state.durable.task(scope)
            if Path(str(source.state["project"])).resolve() != root:
                raise ValueError("change baseline source belongs to another workspace")
            scope = source.state.get("change_scope")
            if not isinstance(scope, str) or not scope:
                raise ValueError("source task has no authoritative change baseline scope")
        return {"change_scope": scope}

    state.durable.transform_task_state(task_id, bind)
    scope = state.durable.task(task_id).state["change_scope"]
    return state.change_tracker_for(root, persistent=True, task_id=scope)
