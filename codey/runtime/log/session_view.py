"""Canonical runtime read model.

Durable entries are parsed exactly once into a SessionView. All runtime
readers (drive, mutation builders, recovery) consume the view instead of
re-parsing entries. SessionView covers the runtime operation projection
only: operation state, effect ledger, delivery ledger. It must never grow
into a session-global view (messages, evidence, ghost, completion).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from codey.runtime.effects.effect_records import (
    EFFECT_CATEGORY_PROVIDER_SEND,
    EFFECT_CATEGORY_TOOL_CALL,
    RuntimeEffectProjection,
    effects_from_entries,
)
from codey.runtime.core.operation_state import (
    LEAF_PROVIDER_EFFECT_PENDING,
    LEAF_TOOL_DELIVERY_PENDING,
    LEAF_TOOL_EFFECT_PENDING,
    RuntimeOperationState,
    operation_state_from_entries,
)
from codey.runtime.log.session_log import RuntimeLogEntry
from codey.runtime.effects.tool_result_delivery import DeliveryBatchProjection, batches_from_entries


@dataclass(frozen=True)
class SessionView:
    state: RuntimeOperationState | None
    effects: tuple[RuntimeEffectProjection, ...]
    batches: tuple[DeliveryBatchProjection, ...]


@dataclass(frozen=True)
class PendingRuntimeFacts:
    """In-flight facts derived from the canonical view, never stored.

    Pending ids are unsettled effect intents; the pending batch is the
    batch that references them. Non-pending leaves derive empty facts.
    """

    effect_category: str = ""
    effect_ids: tuple[str, ...] = ()
    delivery_batch_id: str = ""


def pending_for(view: SessionView) -> PendingRuntimeFacts:
    state = view.state
    if state is None:
        return PendingRuntimeFacts()
    unsettled = tuple(effect for effect in view.effects if effect.settlement is None)
    if state.leaf == LEAF_PROVIDER_EFFECT_PENDING:
        ids = tuple(
            effect.intent.effect_id
            for effect in unsettled
            if effect.intent.effect_category == EFFECT_CATEGORY_PROVIDER_SEND
        )
        batch_id = ""
        if ids:
            batch_id = next(
                (
                    batch.intent.batch_id
                    for batch in view.batches
                    if ids[0] in batch.send_attempts
                ),
                "",
            )
        return PendingRuntimeFacts(
            effect_category=EFFECT_CATEGORY_PROVIDER_SEND,
            effect_ids=ids,
            delivery_batch_id=batch_id,
        )
    if state.leaf == LEAF_TOOL_EFFECT_PENDING:
        ids = tuple(
            effect.intent.effect_id
            for effect in unsettled
            if effect.intent.effect_category == EFFECT_CATEGORY_TOOL_CALL
        )
        batch_id = ""
        if ids:
            wanted = set(ids)
            batch_id = next(
                (
                    batch.intent.batch_id
                    for batch in view.batches
                    if wanted <= set(batch.intent.tool_refs)
                ),
                next(
                    (
                        batch.intent.batch_id
                        for batch in view.batches
                        if ids[0] in batch.intent.tool_refs
                    ),
                    "",
                ),
            )
        return PendingRuntimeFacts(
            effect_category=EFFECT_CATEGORY_TOOL_CALL,
            effect_ids=ids,
            delivery_batch_id=batch_id,
        )
    if state.leaf == LEAF_TOOL_DELIVERY_PENDING:
        batch_id = next(
            (
                batch.intent.batch_id
                for batch in view.batches
                if not batch.is_delivered and not batch.is_recovered
            ),
            "",
        )
        return PendingRuntimeFacts(delivery_batch_id=batch_id)
    return PendingRuntimeFacts()


def load_session_view(
    entries: Iterable[RuntimeLogEntry],
    *,
    session_id: str,
    run_id: str,
) -> SessionView:
    materialized = tuple(entries)
    return SessionView(
        state=operation_state_from_entries(
            materialized,
            session_id=session_id,
            run_id=run_id,
        ),
        effects=effects_from_entries(
            materialized,
            session_id=session_id,
            run_id=run_id,
        ),
        batches=batches_from_entries(
            materialized,
            session_id=session_id,
            run_id=run_id,
        ),
    )


__all__ = ["PendingRuntimeFacts", "SessionView", "load_session_view", "pending_for"]
