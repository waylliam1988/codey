"""Canonical runtime read model.

Durable entries are parsed exactly once into a SessionView. All runtime
readers (drive, mutation builders, recovery) consume the view instead of
re-parsing entries. SessionView covers the runtime operation projection
only: operation state, effect ledger, delivery ledger. It must never grow
into a session-global view (messages, evidence, ghost, completion).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from codey.runtime.core.operation_state import (
    DRIVER_REPAIR,
    DRIVER_WRITER,
    LEAF_PROVIDER_EFFECT_PENDING,
    LEAF_REPAIR_CONTEXT_ADMITTED,
    LEAF_REPAIR_RUNNING,
    LEAF_REPAIR_SETTLED,
    LEAF_TOOL_DELIVERY_PENDING,
    LEAF_TOOL_EFFECT_PENDING,
    RuntimeOperationState,
    RuntimeOperationTransitionError,
    operation_is_open,
    operation_state_from_entries,
)
from codey.runtime.effects.effect_records import (
    EFFECT_CATEGORY_PROVIDER_SEND,
    EFFECT_CATEGORY_TOOL_CALL,
    RuntimeEffectProjection,
    effects_from_entries,
)
from codey.runtime.effects.tool_result_delivery import DeliveryBatchProjection, batches_from_entries
from codey.runtime.log.entries import RuntimeLogEntry


@dataclass(frozen=True)
class SessionView:
    state: RuntimeOperationState | None
    effects: tuple[RuntimeEffectProjection, ...]
    batches: tuple[DeliveryBatchProjection, ...]


@dataclass(frozen=True)
class PendingRuntimeFacts:
    """In-flight facts derived from the canonical view, never stored.

    Pending ids are unsettled effect intents at the state's current turn;
    the pending batch is the current-turn batch that references them.
    Records from older turns are stale history, never current pending
    facts. Non-pending leaves derive empty facts.
    """

    effect_category: str = ""
    effect_ids: tuple[str, ...] = ()
    delivery_batch_id: str = ""


def pending_for(view: SessionView) -> PendingRuntimeFacts:
    state = view.state
    if state is None:
        return PendingRuntimeFacts()
    turn = state.turn
    unsettled = tuple(effect for effect in view.effects if effect.settlement is None)
    current_batches = tuple(batch for batch in view.batches if batch.intent.turn == turn)
    if state.leaf == LEAF_PROVIDER_EFFECT_PENDING:
        ids = tuple(
            effect.intent.effect_id
            for effect in unsettled
            if effect.intent.effect_category == EFFECT_CATEGORY_PROVIDER_SEND
            and effect.intent.turn == turn
        )
        batch_id = ""
        if ids:
            batch_id = next(
                (
                    batch.intent.batch_id
                    for batch in current_batches
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
            and effect.intent.turn == turn
        )
        batch_id = ""
        if ids:
            wanted = set(ids)
            batch_id = next(
                (
                    batch.intent.batch_id
                    for batch in current_batches
                    if wanted <= set(batch.intent.tool_refs)
                ),
                next(
                    (
                        batch.intent.batch_id
                        for batch in current_batches
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
        open_batches = tuple(
            batch
            for batch in current_batches
            if not batch.is_delivered and not batch.is_recovered
        )
        # A same-turn failover may record a fresh batch after a prior
        # provider attempt failed: prefer the untouched batch.
        fresh_batches = tuple(batch for batch in open_batches if not batch.send_attempts)
        if len(fresh_batches) == 1:
            batch_id = fresh_batches[0].intent.batch_id
        elif not fresh_batches and len(open_batches) == 1:
            batch_id = open_batches[0].intent.batch_id
        else:
            # Ambiguous history must fail closed, never grab an arbitrary batch.
            batch_id = ""
        return PendingRuntimeFacts(delivery_batch_id=batch_id)
    return PendingRuntimeFacts()


def require_open_view(projection, view: SessionView) -> RuntimeOperationState:
    if view.state is None:
        raise RuntimeOperationTransitionError("operation state is missing")
    operation_is_open(projection, view.state)
    return view.state


def driver_for_state(state: RuntimeOperationState, *, explicit: str = "") -> str:
    if explicit:
        if explicit not in {DRIVER_WRITER, DRIVER_REPAIR}:
            raise RuntimeOperationTransitionError("driver must be writer or repair")
        return explicit
    if state.driver == DRIVER_REPAIR or state.leaf in {
        LEAF_REPAIR_CONTEXT_ADMITTED,
        LEAF_REPAIR_RUNNING,
        LEAF_REPAIR_SETTLED,
    }:
        return DRIVER_REPAIR
    return DRIVER_WRITER


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


__all__ = [
    "PendingRuntimeFacts",
    "SessionView",
    "driver_for_state",
    "load_session_view",
    "pending_for",
    "require_open_view",
]
