"""Pure delivery-recovery write builder.

The builder reads one SessionView plus its arguments and returns the
bounded rows to commit. It never touches the session lock; the mutation
line commits its rows atomically.
"""

from __future__ import annotations

from codey.runtime.core.operation_state import (
    LEAF_TOOL_DELIVERY_PENDING,
    RuntimeOperationTransitionError,
    mark_tool_delivery_settled,
    operation_state_entry,
)
from codey.runtime.effects.tool_result_delivery import recovered_entry
from codey.runtime.log.session_view import (
    SessionView,
    _require_open_view,
    pending_for,
)


def _build_delivery_recovered_rows(
    projection,
    view: SessionView,
    *,
    session_id: str,
    run_id: str,
    batch_id: str,
    recovered_ids: tuple[str, ...],
    recovered_reads: int = 0,
    recovered_lookups: int = 0,
) -> tuple[dict[str, object], ...]:
    state = _require_open_view(projection, view)
    batches = view.batches
    projection_batch = next(
        (batch for batch in batches if batch.intent.batch_id == batch_id),
        None,
    )
    if projection_batch is None:
        raise RuntimeOperationTransitionError(
            "delivery recovery requires matching delivery_pending state"
        )
    pending = pending_for(view)
    if (
        state.leaf != LEAF_TOOL_DELIVERY_PENDING
        or pending.delivery_batch_id != batch_id
    ):
        if not projection_batch.is_recovered:
            raise RuntimeOperationTransitionError(
                "delivery recovery requires matching delivery_pending state"
            )
        entry = recovered_entry(
            session_id,
            run_id,
            batch_id=batch_id,
            recovered_effect_ids=recovered_ids,
            recovered_reads=recovered_reads,
            recovered_lookups=recovered_lookups,
            batches=batches,
        )
        return () if entry is None else (entry,)
    next_state = mark_tool_delivery_settled(state)
    rows: list[dict[str, object]] = []
    entry = recovered_entry(
        session_id,
        run_id,
        batch_id=batch_id,
        recovered_effect_ids=recovered_ids,
        recovered_reads=recovered_reads,
        recovered_lookups=recovered_lookups,
        batches=batches,
    )
    if entry is not None:
        rows.append(entry)
    rows.append(operation_state_entry(next_state))
    return tuple(rows)
