"""Pure tool-batch write builders.

Each builder reads one SessionView plus its arguments and returns the
typed changes to commit. Builders never touch the session lock; the
mutation line commits them atomically.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from codey.runtime.core.operation_payload import RuntimeOperationTransitionError
from codey.runtime.core.operation_state import (
    mark_tool_delivery_pending,
    mark_tool_effect_pending,
    mark_tool_effect_settled,
)
from codey.runtime.effects.effect_records import (
    RuntimeEffectIntent,
    RuntimeEffectSettlement,
    find_effect,
    prepare_intent,
    prepare_settlement,
    require_new_effect_id,
)
from codey.runtime.effects.tool_result_delivery import (
    DeliveryBatchIntent,
    DeliveryBatchProjection,
    prepare_batch_intent,
)
from codey.runtime.log.session_view import (
    SessionView,
    driver_for_state,
    pending_for,
    require_open_view,
)
from codey.runtime.write.checkpoint import RuntimeChange, RuntimeChanges


@dataclass(frozen=True)
class ToolBatchCommit:
    batch_id: str
    effect_ids: tuple[str, ...]
    driver: str


def build_tool_batch_changes(
    view: SessionView,
    *,
    session_id: str,
    run_id: str,
    intents: Iterable[RuntimeEffectIntent],
    delivery_intent: DeliveryBatchIntent | None,
    driver: str = "",
) -> tuple[RuntimeChanges, ToolBatchCommit]:
    state = require_open_view(view)
    effect_driver = driver_for_state(state, explicit=driver)
    existing_effects = view.effects
    prepared_intents = tuple(
        prepare_intent(session_id, run_id, intent) for intent in intents
    )
    for intent in prepared_intents:
        require_new_effect_id(existing_effects, intent.effect_id)
    changes: list[RuntimeChange] = list(prepared_intents)
    if delivery_intent is None:
        if not prepared_intents:
            raise RuntimeOperationTransitionError("nested batch requires effect intents")
        for intent in prepared_intents:
            parent = find_effect(existing_effects, intent.parent_effect_id)
            if (parent.settlement is not None or parent.intent.tool_name != "codemode"
                    or parent.intent.turn != intent.turn):
                raise RuntimeOperationTransitionError("nested effect requires its pending CodeMode parent")
        changes.append(mark_tool_effect_pending(state, driver=effect_driver, turn=prepared_intents[0].turn))
        return tuple(changes), ToolBatchCommit("", tuple(i.effect_id for i in prepared_intents), effect_driver)
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
    if existing_batch is None:
        changes.append(DeliveryBatchProjection(prepared_delivery))
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
    changes.append(next_state)
    batch_commit = ToolBatchCommit(
        batch_id=prepared_delivery.batch_id,
        effect_ids=effect_ids,
        driver=effect_driver,
    )
    return tuple(changes), batch_commit


def build_tool_settle_changes(
    view: SessionView,
    *,
    session_id: str,
    run_id: str,
    settlement: RuntimeEffectSettlement,
) -> tuple[RuntimeChanges, RuntimeEffectSettlement]:
    state = require_open_view(view)
    effects = view.effects
    matching = find_effect(effects, settlement.effect_id)
    prepared = prepare_settlement(session_id, run_id, settlement, effects)
    if matching.settlement is not None:
        return (), prepared
    changes: list[RuntimeChange] = [prepared]
    pending = pending_for(view)
    if prepared.effect_id not in pending.effect_ids:
        raise RuntimeOperationTransitionError("effect is not pending on operation state")
    remaining = tuple(effect_id for effect_id in pending.effect_ids if effect_id != prepared.effect_id)
    changes.append(
        mark_tool_effect_settled(state, has_remaining=bool(remaining))
    )
    return tuple(changes), prepared


__all__ = [
    "ToolBatchCommit",
    "build_tool_batch_changes",
    "build_tool_settle_changes",
]
