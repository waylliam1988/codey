"""Manual-drive helpers for durable operation state."""

from __future__ import annotations

from codey.runtime.core.operation_reducer import RuntimeAction, next_runtime_action
from codey.runtime.log.session_log import RuntimeSessionLog
from codey.runtime.log.session_view import load_session_view


def peek_next_action(
    session_log: RuntimeSessionLog,
    *,
    session_id: str,
    run_id: str,
) -> RuntimeAction:
    """Return the next reducer action without committing or executing effects."""
    view = load_session_view(
        session_log.entries(session_id),
        session_id=session_id,
        run_id=run_id,
    )
    return next_runtime_action(view)


__all__ = ["peek_next_action"]
