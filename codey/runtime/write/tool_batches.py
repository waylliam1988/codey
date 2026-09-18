"""Pure tool-batch write builders.

Each builder reads one SessionView plus its arguments and returns the
bounded rows to commit. Builders never touch the session lock; the
mutation line commits their rows atomically.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from codey.runtime.core.operation_state import (
    RuntimeOperationTransitionError,
    mark_tool_delivery_pending,
    mark_tool_effect_pending,
    mark_tool_effect_settled,
    operation_state_entry,
)
from codey.runtime.effects.effect_records import (
    RuntimeEffectIntent,
    RuntimeEffectSettlement,
    effect_intent_entry,
    effect_settlement_entry,
    find_effect,
    prepare_intent,
    prepare_settlement,
    require_new_effect_id,
)
from codey.runtime.effects.tool_result_delivery import (
    DeliveryBatchIntent,
    batch_intent_entry,
    prepare_batch_intent,
)
from codey.runtime.log.session_view import (
    SessionView,
    driver_for_state,
    pending_for,
    require_open_view,
)


@dataclass(frozen=True)
class ToolBatchCommit:
    batch_id: str
    effect_ids: tuple[str, ...]
    driver: str


def build_tool_batch_rows(
    projection,
    view: SessionView,
    *,
    session_id: str,
    run_id: str,
    intents: Iterable[RuntimeEffectIntent],
    delivery_intent: DeliveryBatchIntent,
    driver: str = "",
) -> tuple[tuple[dict[str, object], ...], ToolBatchCommit]:
    state = require_open_view(projection, view)
    effect_driver = driver_for_state(state, explicit=driver)
    existing_effects = view.effects
    prepared_intents = tuple(
        prepare_intent(session_id, run_id, intent) for intent in intents
    )
    for intent in prepared_intents:
        require_new_effect_id(existing_effects, intent.effect_id)
    prepared_delivery = prepare_batch_intent(session_id, run_id, delivery_intent)
    batches = view.batches
    existing_batch = next(
        (
            batch
            for batch in batches
            if batch.intent.batch_id == prepared_delivery.batch_id
        ),
        None,
    )
    rows = [effect_intent_entry(intent) for intent in prepared_intents]
    if existing_batch is None:
        rows.append(batch_intent_entry(prepared_delivery))
    elif (
        existing_batch.intent.turn != prepared_delivery.turn
        or existing_batch.intent.items != prepared_delivery.items
        or existing_batch.intent.batch_digest != prepared_delivery.batch_digest
    ):
        raise RuntimeOperationTransitionError("delivery batch intent conflict")

    effect_ids = tuple(intent.effect_id for intent in prepared_intents)
    if effect_ids:
        next_state = mark_tool_effect_pending(
            state,
            driver=effect_driver,
            turn=prepared_delivery.turn,
        )
    else:
        next_state = mark_tool_delivery_pending(
            state,
            driver=effect_driver,
            turn=prepared_delivery.turn,
        )
    rows.append(operation_state_entry(next_state))
    batch_commit = ToolBatchCommit(
        batch_id=prepared_delivery.batch_id,
        effect_ids=effect_ids,
        driver=effect_driver,
    )
    return tuple(rows), batch_commit


def build_tool_settle_rows(
    projection,
    view: SessionView,
    *,
    session_id: str,
    run_id: str,
    settlement: RuntimeEffectSettlement,
) -> tuple[tuple[dict[str, object], ...], RuntimeEffectSettlement]:
    state = require_open_view(projection, view)
    effects = view.effects
    matching = find_effect(effects, settlement.effect_id)
    prepared = prepare_settlement(session_id, run_id, settlement, effects)
    if matching.settlement is not None:
        return (), prepared
    rows = [effect_settlement_entry(prepared)]
    pending = pending_for(view)
    if prepared.effect_id not in pending.effect_ids:
        raise RuntimeOperationTransitionError("effect is not pending on operation state")
    remaining = tuple(effect_id for effect_id in pending.effect_ids if effect_id != prepared.effect_id)
    rows.append(
        operation_state_entry(
            mark_tool_effect_settled(state, has_remaining=bool(remaining))
        )
    )
    return tuple(rows), prepared


__all__ = [
    "ToolBatchCommit",
    "build_tool_batch_rows",
    "build_tool_settle_rows",
]
