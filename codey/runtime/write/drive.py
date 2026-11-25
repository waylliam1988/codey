"""Manual-drive helpers for durable operation state."""

from __future__ import annotations

from typing import TYPE_CHECKING

from codey.runtime.core.operation_reducer import RuntimeAction, next_runtime_action
from codey.runtime.write.checkpoint import view_from_checkpoint

if TYPE_CHECKING:
    from codey.durable.session import DurableSession


def peek_next_action(
    durable: DurableSession,
    *,
    session_id: str,
    run_id: str,
) -> RuntimeAction:
    """Return the next reducer action without committing or executing effects."""
    task, effects = durable.task_facts(run_id)
    return next_runtime_action(view_from_checkpoint(task, effects, session_id=session_id))


__all__ = ["peek_next_action"]
