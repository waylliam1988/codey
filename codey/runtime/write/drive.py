"""Manual-drive helpers for durable operation state."""

from __future__ import annotations

from typing import TYPE_CHECKING

from codey.durable.checkpoint import checkpoint_entries
from codey.runtime.core.operation_reducer import RuntimeAction, next_runtime_action
from codey.runtime.log.session_view import load_session_view

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
    entries = checkpoint_entries(task, effects, session_id=session_id)
    view = load_session_view(
        entries,
        session_id=session_id,
        run_id=run_id,
    )
    return next_runtime_action(view)


__all__ = ["peek_next_action"]
