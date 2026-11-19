"""Versioned read projection of the authoritative task checkpoint."""
from __future__ import annotations

from codey.durable.model import TaskCheckpoint
from codey.types.json_value import JsonObject


def task_lifecycle(checkpoint: TaskCheckpoint) -> JsonObject:
    return {
        "run_id": checkpoint.task_id,
        "session_id": checkpoint.conversation_id,
        "task_revision": checkpoint.revision,
        "task_status": str(checkpoint.status),
        "execution_kind": checkpoint.execution_kind,
        "owner_task_id": checkpoint.owner_task_id,
        "stop_reason": str(checkpoint.state.get("stop_reason") or ""),
    }

