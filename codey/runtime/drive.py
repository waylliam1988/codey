"""Manual-drive helpers for durable operation state."""

from __future__ import annotations

from codey.runtime.effect_records import effects_from_entries
from codey.runtime.operation_reducer import RuntimeAction, next_runtime_action
from codey.runtime.operation_state import operation_state_from_entries
from codey.runtime.session_log import RuntimeSessionLog
from codey.runtime.tool_result_delivery import batches_from_entries


def peek_next_action(
    session_log: RuntimeSessionLog,
    *,
    session_id: str,
    run_id: str,
) -> RuntimeAction:
    """Return the next reducer action without committing or executing effects."""
    entries = session_log.entries(session_id)
    state = operation_state_from_entries(
        entries,
        session_id=session_id,
        run_id=run_id,
    )
    effects = effects_from_entries(entries, session_id=session_id, run_id=run_id)
    batches = batches_from_entries(entries, session_id=session_id, run_id=run_id)
    return next_runtime_action(
        state,
        effects=effects,
        delivery_batches=batches,
    )


__all__ = ["peek_next_action"]
