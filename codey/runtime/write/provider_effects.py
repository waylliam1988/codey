"""Pure provider-effect write builders.

Each builder reads one SessionView plus its arguments and returns the
bounded rows to commit. Builders never touch the session lock; the
mutation line commits their rows atomically.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from codey.runtime.core.operation_payload import RuntimeExecutionCounters
from codey.runtime.core.operation_reducer import ACTION_FAIL_INVARIANT, admit_request_counters, next_runtime_action
from codey.runtime.core.operation_state import (
    LEAF_PROVIDER_REQUEST_READY,
    RuntimeOperationState,
    RuntimeOperationTransitionError,
    mark_provider_effect_pending,
    mark_provider_effect_settled,
    mark_provider_request_ready,
    operation_state_entry,
)
from codey.runtime.effects.effect_records import (
    EFFECT_CATEGORY_PROVIDER_SEND,
    SENT_STATE_NOT_SENT,
    SENT_STATE_SETTLED,
    SETTLEMENT_STATUS_ERROR,
    RuntimeEffectError,
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
    delivered_entry,
    send_attempt_entry,
    send_superseded_entry,
)
from codey.runtime.log.session_view import (
    SessionView,
    driver_for_state,
    require_open_view,
)


@dataclass(frozen=True)
class ProviderAdmission:
    intent: RuntimeEffectIntent
    newly_admitted: bool


def build_provider_prepare_rows(
    projection: Any, view: SessionView, *, expected: RuntimeOperationState,
    request_ref: str, turn: int, logical_turn: int | None = None,
) -> tuple[dict[str, object], ...]:
    state = require_open_view(projection, view)
    if state.leaf == LEAF_PROVIDER_REQUEST_READY and state.request_ref == request_ref:
        return ()
    if state != expected:
        raise RuntimeEffectError("operation changed before request preparation")
    decision = next_runtime_action(view)
    if decision.kind == ACTION_FAIL_INVARIANT:
        raise RuntimeEffectError(f"request preparation violates operation facts: {decision.reason}")
    expected_turn = max((effect.intent.turn for effect in view.effects
                         if effect.intent.effect_category == EFFECT_CATEGORY_PROVIDER_SEND), default=0) + 1
    if turn != expected_turn:
        raise RuntimeEffectError("prepared request must retain the next provider send identity")
    if any(effect.settlement is None and effect.intent.effect_category != EFFECT_CATEGORY_PROVIDER_SEND
           for effect in view.effects):
        raise RuntimeEffectError("request preparation cannot bypass pending tools")
    prepared = mark_provider_request_ready(state, request_ref=request_ref,
                                          driver=driver_for_state(state), turn=turn)
    prepared = replace(prepared, response_ref=state.response_ref if any(
        effect.settlement is None and effect.intent.effect_category == EFFECT_CATEGORY_PROVIDER_SEND
        for effect in view.effects) else "",
        logical_turn=state.logical_turn if logical_turn is None else logical_turn)
    return (operation_state_entry(prepared),)


def _require_supersedable_not_sent(
    view: SessionView,
    supersede_effect_id: str,
) -> None:
    """Prove the voided attempt deterministically sent nothing usable.

    A matching send_attempt alone is not enough: only a provider send whose
    settlement exists with an error status and a NOT_SENT state may be voided.
    Anything else (unsettled, maybe-sent, ok) keeps the batch locked.
    """
    if not supersede_effect_id:
        raise RuntimeOperationTransitionError("supersede requires a failed provider effect id")
    try:
        matching = find_effect(view.effects, supersede_effect_id)
    except Exception as exc:
        raise RuntimeOperationTransitionError(
            f"supersede requires a known provider effect: {supersede_effect_id!r}"
        ) from exc
    if matching.intent.effect_category != EFFECT_CATEGORY_PROVIDER_SEND:
        raise RuntimeOperationTransitionError(
            f"supersede requires a provider send effect: {supersede_effect_id!r}"
        )
    settlement = matching.settlement
    if (
        settlement is None
        or settlement.status != SETTLEMENT_STATUS_ERROR
        or settlement.sent_state != SENT_STATE_NOT_SENT
    ):
        raise RuntimeOperationTransitionError(
            f"supersede requires an error/NOT_SENT settlement: {supersede_effect_id!r}"
        )


def build_provider_begin_rows(
    projection: Any,
    view: SessionView,
    *,
    session_id: str,
    run_id: str,
    intent: RuntimeEffectIntent,
    driver: str = "",
    delivery_batch_id: str = "",
    supersede_effect_id: str = "",
    counters: RuntimeExecutionCounters | None = None,
    expected_counters: RuntimeExecutionCounters | None = None,
    expected_request_ref: str = "",
) -> tuple[tuple[dict[str, object], ...], RuntimeEffectIntent]:
    state = require_open_view(projection, view)
    prepared = prepare_intent(session_id, run_id, intent)
    if expected_request_ref:
        if prepared.request_ref != expected_request_ref:
            raise RuntimeEffectError("provider admission has a different frozen request")
        existing = next((effect for effect in view.effects if effect.intent.effect_id == prepared.effect_id), None)
        if existing is not None:
            if existing.intent != prepared:
                raise RuntimeEffectError("prepared provider identity conflicts with the admitted effect")
            return (), existing.intent
        if (state.leaf != LEAF_PROVIDER_REQUEST_READY or state.request_ref != expected_request_ref
                or state.response_ref or any(effect.settlement is None for effect in view.effects)
                or prepared.turn != state.turn or prepared.provider_id != state.provider_id
                or ((prepared.phase == "repair") != (state.driver == "repair"))):
            raise RuntimeEffectError("provider admission requires its prepared request after the previous boundary")
    if counters is not None:
        if state.counters != expected_counters:
            raise RuntimeEffectError("provider request counters changed before admission")
        state = replace(state, counters=admit_request_counters(state.counters, counters))
    if delivery_batch_id and prepared.delivery_batch_id != delivery_batch_id:
        from dataclasses import replace as _replace

        prepared = _replace(prepared, delivery_batch_id=delivery_batch_id)
    require_new_effect_id(view.effects, prepared.effect_id)
    effect_driver = driver_for_state(state, explicit=driver)
    batches = view.batches
    rows = [effect_intent_entry(prepared)]
    if delivery_batch_id:
        if supersede_effect_id:
            _require_supersedable_not_sent(view, supersede_effect_id)
            voided = send_superseded_entry(
                session_id,
                run_id,
                batch_id=delivery_batch_id,
                provider_effect_id=supersede_effect_id,
                batches=batches,
            )
            if voided is not None:
                rows.append(voided)
            from dataclasses import replace as _replace

            batches = tuple(
                _replace(
                    batch,
                    superseded_effect_ids=(*batch.superseded_effect_ids, supersede_effect_id),
                )
                if batch.intent.batch_id == delivery_batch_id
                else batch
                for batch in batches
            )
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
    projection: Any,
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
    # The provider intent owns the delivery batch it was attempting to
    # acknowledge.  At a generation boundary the current operation leaf may
    # already point at the next tool batch, so deriving this from
    # ``pending_for(view)`` can mark the wrong batch (or lose the receipt).
    delivery_batch_id = matching.intent.delivery_batch_id
    pending_batch = next(
        (batch for batch in view.batches if batch.intent.batch_id == delivery_batch_id),
        None,
    )
    if (
        pending_batch is not None
        and prepared.status == "ok"
        and prepared.sent_state == SENT_STATE_SETTLED
        # A provider settlement closes the generation that produced the
        # current tool batch.  The batch is marked delivered only when this
        # provider effect actually carried its delivery attempt; a deferred
        # generation has no such attempt yet and must remain recoverable.
        and prepared.effect_id in pending_batch.active_attempts
    ):
        batches = view.batches
        delivered = delivered_entry(
            session_id,
            run_id,
            batch_id=delivery_batch_id,
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


def build_delivery_delivered_rows(
    projection: Any,
    view: SessionView,
    *,
    session_id: str,
    run_id: str,
    batch_id: str,
    provider_effect_id: str,
) -> tuple[dict[str, object], ...]:
    """Record a provider ledger witness as a local delivery receipt."""
    batches = view.batches
    entry = delivered_entry(
        session_id, run_id, batch_id=batch_id,
        provider_effect_id=provider_effect_id, batches=batches,
    )
    return (entry,) if entry is not None else ()


__all__ = [
    "build_provider_begin_rows",
    "build_provider_settle_rows",
    "build_delivery_delivered_rows",
]
