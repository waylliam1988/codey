"""Pure provider-effect write builders.

Each builder reads one SessionView plus its arguments and returns the
bounded rows to commit. Builders never touch the session lock; the
mutation line commits their rows atomically.
"""

from __future__ import annotations

from codey.runtime.core.operation_state import (
    mark_provider_effect_pending,
    mark_provider_effect_settled,
    operation_state_entry,
)
from codey.runtime.effects.effect_records import (
    RuntimeEffectIntent,
    RuntimeEffectSettlement,
    SENT_STATE_SETTLED,
    effect_intent_entry,
    effect_settlement_entry,
    find_effect,
    prepare_intent,
    prepare_settlement,
    require_new_effect_id,
)
from codey.runtime.effects.tool_result_delivery import (
    delivered_entry,
    send_attempt_entry,
)
from codey.runtime.log.session_view import (
    SessionView,
    driver_for_state,
    pending_for,
    require_open_view,
)


def build_provider_begin_rows(
    projection,
    view: SessionView,
    *,
    session_id: str,
    run_id: str,
    intent: RuntimeEffectIntent,
    driver: str = "",
    delivery_batch_id: str = "",
) -> tuple[tuple[dict[str, object], ...], RuntimeEffectIntent]:
    state = require_open_view(projection, view)
    prepared = prepare_intent(session_id, run_id, intent)
    require_new_effect_id(view.effects, prepared.effect_id)
    effect_driver = driver_for_state(state, explicit=driver)
    batches = view.batches
    rows = [effect_intent_entry(prepared)]
    if delivery_batch_id:
        attempt = send_attempt_entry(
            session_id,
            run_id,
            batch_id=delivery_batch_id,
            provider_effect_id=prepared.effect_id,
            batches=batches,
        )
        if attempt is not None:
            rows.append(attempt)
    rows.append(
        operation_state_entry(
            mark_provider_effect_pending(
                state,
                driver=effect_driver,
                provider_id=prepared.provider_id,
                turn=prepared.turn,
            )
        )
    )
    return tuple(rows), prepared


def build_provider_settle_rows(
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
    pending = pending_for(view)
    rows = [effect_settlement_entry(prepared)]
    if (
        pending.delivery_batch_id
        and prepared.status == "ok"
        and prepared.sent_state == SENT_STATE_SETTLED
    ):
        batches = view.batches
        delivered = delivered_entry(
            session_id,
            run_id,
            batch_id=pending.delivery_batch_id,
            provider_effect_id=prepared.effect_id,
            batches=batches,
        )
        if delivered is not None:
            rows.append(delivered)
    rows.append(
        operation_state_entry(
            mark_provider_effect_settled(state)
        )
    )
    return tuple(rows), prepared


__all__ = [
    "build_provider_begin_rows",
    "build_provider_settle_rows",
]
