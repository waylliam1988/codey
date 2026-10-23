"""Read-only lifecycle versions from the existing durable task checkpoint."""
from __future__ import annotations

from typing import Any


def task_lifecycle(checkpoint: Any) -> dict[str, Any]:
    return {
        "run_id": checkpoint.task_id,
        "session_id": checkpoint.conversation_id,
        "task_revision": checkpoint.revision,
        "task_status": str(checkpoint.status),
        "stop_reason": str(checkpoint.state.get("stop_reason") or ""),
    }


def version_task_event(durable: Any, event: dict[str, Any]) -> dict[str, Any]:
    """Capture once, before publication/storage; replay never reads a new version."""
    payload = dict(event)
    if payload.get("type") not in {"task_start", "task_done"} or "task_revision" in payload:
        return payload
    try:
        checkpoint = durable.task(str(payload.get("run_id") or ""))
    except (KeyError, AttributeError):
        return payload  # Non-durable test/headless adapters have no checkpoint.
    payload.update(task_revision=checkpoint.revision, task_status=str(checkpoint.status))
    return payload


def snapshot_tasks(durable: Any, payload: dict[str, Any]) -> list[dict[str, Any]]:
    ids = {str(row.get("run_id") or "") for row in payload.get("active_runs", ())}
    ids.add(str(payload.get("run_id") or ""))
    for row in payload.get("shell_approvals", ()):
        ids.add(str(row.get("source_run_id") or row.get("run_id") or ""))
        ids.add(str(row.get("continuation_run_id") or ""))
    last = payload.get("last_terminal_event") or {}
    ids.add(str(last.get("run_id") or ""))
    rows = []
    for task_id in sorted(ids - {""}):
        try:
            rows.append(task_lifecycle(durable.task(task_id)))
        except (KeyError, AttributeError):
            continue
    return rows
