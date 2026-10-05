"""Pure delivery-recovery write builder.

The builder reads one SessionView plus its arguments and returns the
bounded rows to commit. It never touches the session lock; the mutation
line commits its rows atomically.
"""

from __future__ import annotations

from typing import Any

from codey.runtime.core.operation_state import (
    LEAF_TOOL_DELIVERY_PENDING,
    RuntimeOperationTransitionError,
)
from codey.runtime.effects.tool_result_delivery import recovered_entry
from codey.runtime.log.session_view import (
    SessionView,
    pending_for,
    require_open_view,
)


def build_delivery_recovered_rows(
    projection: Any,
    view: SessionView,
    *,
    session_id: str,
    run_id: str,
    batch_id: str,
    recovered_ids: tuple[str, ...],
    recovered_reads: int = 0,
    recovered_lookups: int = 0,
) -> tuple[dict[str, object], ...]:
    state = require_open_view(projection, view)
    batches = view.batches
    projection_batch = next(
        (batch for batch in batches if batch.intent.batch_id == batch_id),
        None,
    )
    if projection_batch is None:
        raise RuntimeOperationTransitionError(
            "delivery recovery requires matching delivery_pending state"
        )
    if projection_batch.is_abandoned or projection_batch.is_delivered:
        raise RuntimeOperationTransitionError(
            "delivery recovery requires matching delivery_pending state"
        )
    pending = pending_for(view)
    if (
        state.leaf != LEAF_TOOL_DELIVERY_PENDING
        or pending.delivery_batch_id != batch_id
    ):
        raise RuntimeOperationTransitionError(
            "delivery recovery requires matching delivery_pending state"
        )
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
    return tuple(rows)


__all__ = ["build_delivery_recovered_rows"]
