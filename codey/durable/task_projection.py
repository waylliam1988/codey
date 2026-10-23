"""Versioned read projection of the authoritative task checkpoint."""
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

