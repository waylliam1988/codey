"""Serialized runtime mutation boundary.

Every production mutation of durable operation state, effect ledger, delivery
ledger, and operation settlement goes through this line. The callback passed to
``RuntimeSessionLog.mutate`` stays pure: it reads the current projection and
entries, decides the next bounded records, and commits them as one batch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

from codey.runtime.effect_records import (
    EFFECT_CATEGORY_PROVIDER_SEND,
    EFFECT_CATEGORY_TOOL_CALL,
    RuntimeEffectError,
    RuntimeEffectIntent,
    RuntimeEffectProjection,
    RuntimeEffectSettlement,
    SENT_STATE_SETTLED,
    effect_intent_entry,
    effect_settlement_entry,
    effects_from_entries,
    prepare_intent,
    prepare_settlement,
)
from codey.runtime.operation_state import (
    DRIVER_REPAIR,
    DRIVER_WRITER,
    LEAF_REPAIR_CONTEXT_ADMITTED,
    LEAF_REPAIR_RUNNING,
    LEAF_REPAIR_SETTLED,
    LEAF_TERMINAL,
    LEAF_TOOL_DELIVERY_PENDING,
    RuntimeOperationState,
    RuntimeOperationTransitionError,
    mark_completion_blocked as state_mark_completion_blocked,
    mark_completion_proof_recorded as state_mark_completion_proof_recorded,
    mark_provider_effect_pending,
    mark_provider_effect_settled,
    mark_repair_context_admitted as state_mark_repair_context_admitted,
    mark_repair_running as state_mark_repair_running,
    mark_repair_settled as state_mark_repair_settled,
    mark_terminal as state_mark_terminal,
    mark_tool_delivery_pending,
    mark_tool_effect_pending,
    mark_tool_effect_settled,
    mark_writer_running as state_mark_writer_running,
    mark_writer_settled as state_mark_writer_settled,
    new_operation_state,
    operation_is_open,
    operation_state_entry,
    operation_state_from_entries,
    outcome_for_terminal,
    start_entries,
)
from codey.runtime.session_log import RuntimeLogEntry, RuntimeSessionLog
from codey.runtime.tool_result_delivery import (
    DeliveryBatchIntent,
    batch_intent_entry,
    batches_from_entries,
    delivered_entry,
    prepare_batch_intent,
    recovered_entry,
    send_attempt_entry,
)


@dataclass(frozen=True)
class ToolBatchCommit:
    batch_id: str
    effect_ids: tuple[str, ...]
    driver: str


class RuntimeMutationLine:
    """Single writer-facing API for runtime durable mutations."""

    def __init__(self, session_log: RuntimeSessionLog) -> None:
        self.session_log = session_log

    def accept_operation(
        self,
        *,
        session_id: str,
        run_id: str,
        project: object = "",
        provider_id: str,
        turn_budget: int,
        max_repair_rounds: int,
        task_kind: str = "task",
    ) -> RuntimeOperationState | None:
        accepted: RuntimeOperationState | None = None

        def mutation(projection, entries):
            nonlocal accepted
            existing = operation_state_from_entries(
                entries,
                session_id=session_id,
                run_id=run_id,
            )
            if existing is not None:
                operation_is_open(projection, existing)
                accepted = existing
                return ()
            state = new_operation_state(
                session_id=session_id,
                run_id=run_id,
                project=project,
                provider_id=provider_id,
                turn_budget=turn_budget,
                max_repair_rounds=max_repair_rounds,
                task_kind=task_kind,
            )
            accepted = state
            return start_entries(projection, state)

        self.session_log.mutate(session_id, mutation)
        return accepted

    def _commit_state(
        self,
        session_id: str,
        run_id: str,
        transition: Callable[[RuntimeOperationState], RuntimeOperationState],
    ) -> RuntimeOperationState | None:
        committed: RuntimeOperationState | None = None

        def mutation(projection, entries):
            nonlocal committed
            current = _require_state(entries, session_id=session_id, run_id=run_id)
            op = projection.operations.get(current.operation_id)
            if op is not None and op.status != "open":
                if current.leaf == LEAF_TERMINAL:
                    next_state = transition(current)
                    if next_state == current:
                        committed = current
                        return ()
                    raise RuntimeOperationTransitionError("terminal operation is immutable")
                raise RuntimeOperationTransitionError("operation already settled")
            operation_is_open(projection, current)
            next_state = transition(current)
            if next_state == current:
                committed = current
                return ()
            rows = [operation_state_entry(next_state)]
            if next_state.leaf == LEAF_TERMINAL:
                rows.append(_operation_settled_entry(next_state))
            committed = next_state
            return tuple(rows)

        self.session_log.mutate(session_id, mutation)
        return committed

    def mark_writer_running(
        self,
        session_id: str,
        run_id: str,
        *,
        provider_id: str,
        writer_attempt: int = 1,
    ) -> RuntimeOperationState | None:
        return self._commit_state(
            session_id,
            run_id,
            lambda state: state_mark_writer_running(
                state,
                provider_id=provider_id,
                writer_attempt=writer_attempt,
            ),
        )

    def mark_writer_settled(
        self,
        session_id: str,
        run_id: str,
        *,
        provider_id: str,
        turns_used: int,
        stop_reason: str,
    ) -> RuntimeOperationState | None:
        return self._commit_state(
            session_id,
            run_id,
            lambda state: state_mark_writer_settled(
                state,
                provider_id=provider_id,
                turns_used=turns_used,
                stop_reason=stop_reason,
            ),
        )

    def record_completion_proof(
        self,
        session_id: str,
        run_id: str,
        *,
        proof_ref: str,
        proof_status: str,
        proof_satisfied: bool,
    ) -> RuntimeOperationState | None:
        return self._commit_state(
            session_id,
            run_id,
            lambda state: state_mark_completion_proof_recorded(
                state,
                proof_ref=proof_ref,
                proof_status=proof_status,
                proof_satisfied=proof_satisfied,
            ),
        )

    def admit_repair_context(
        self,
        session_id: str,
        run_id: str,
        *,
        context_ref: str,
    ) -> RuntimeOperationState | None:
        return self._commit_state(
            session_id,
            run_id,
            lambda state: state_mark_repair_context_admitted(
                state,
                context_ref=context_ref,
            ),
        )

    def mark_repair_running(
        self,
        session_id: str,
        run_id: str,
        *,
        provider_id: str,
    ) -> RuntimeOperationState | None:
        return self._commit_state(
            session_id,
            run_id,
            lambda state: state_mark_repair_running(state, provider_id=provider_id),
        )

    def mark_repair_settled(
        self,
        session_id: str,
        run_id: str,
        *,
        provider_id: str,
        stop_reason: str,
        blocked_reason: str = "",
        turns_used: int | None = None,
    ) -> RuntimeOperationState | None:
        return self._commit_state(
            session_id,
            run_id,
            lambda state: state_mark_repair_settled(
                state,
                provider_id=provider_id,
                stop_reason=stop_reason,
                blocked_reason=blocked_reason,
                turns_used=turns_used,
            ),
        )

    def mark_completion_blocked(
        self,
        session_id: str,
        run_id: str,
        *,
        reason: str,
    ) -> RuntimeOperationState | None:
        return self._commit_state(
            session_id,
            run_id,
            lambda state: state_mark_completion_blocked(state, reason=reason),
        )

    def mark_terminal(
        self,
        session_id: str,
        run_id: str,
        *,
        stop_reason: str,
        summary_chars: int,
        turns: int,
        max_turns: int,
        provider: str,
        blocked_reason: str | None = None,
    ) -> RuntimeOperationState | None:
        return self._commit_state(
            session_id,
            run_id,
            lambda state: state_mark_terminal(
                state,
                stop_reason=stop_reason,
                summary_chars=summary_chars,
                turns=turns,
                max_turns=max_turns,
                provider=provider,
                blocked_reason=blocked_reason,
            ),
        )

    def begin_provider_effect(
        self,
        session_id: str,
        run_id: str,
        intent: RuntimeEffectIntent,
        *,
        driver: str = "",
        delivery_batch_id: str = "",
    ) -> RuntimeEffectIntent:
        committed: RuntimeEffectIntent | None = None

        def mutation(projection, entries):
            nonlocal committed
            state = _require_open_state(
                projection,
                entries,
                session_id=session_id,
                run_id=run_id,
            )
            prepared = prepare_intent(session_id, run_id, intent)
            _require_new_effect_id(
                effects_from_entries(entries, session_id=session_id, run_id=run_id),
                prepared.effect_id,
            )
            effect_driver = _driver_for_state(state, explicit=driver)
            batches = batches_from_entries(entries, session_id=session_id, run_id=run_id)
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
                        effect_id=prepared.effect_id,
                        driver=effect_driver,
                        provider_id=prepared.provider_id,
                        turn=prepared.turn,
                        delivery_batch_id=delivery_batch_id,
                    )
                )
            )
            committed = prepared
            return tuple(rows)

        self.session_log.mutate(session_id, mutation)
        if committed is None:
            raise RuntimeEffectError("provider effect intent was not committed")
        return committed

    def settle_provider_effect(
        self,
        session_id: str,
        run_id: str,
        settlement: RuntimeEffectSettlement,
    ) -> RuntimeEffectSettlement:
        committed: RuntimeEffectSettlement | None = None

        def mutation(projection, entries):
            nonlocal committed
            state = _require_open_state(
                projection,
                entries,
                session_id=session_id,
                run_id=run_id,
            )
            effects = effects_from_entries(entries, session_id=session_id, run_id=run_id)
            matching = _find_effect(effects, settlement.effect_id)
            prepared = prepare_settlement(session_id, run_id, settlement, effects)
            if matching.settlement is not None:
                committed = prepared
                return ()
            rows = [effect_settlement_entry(prepared)]
            if (
                state.pending_delivery_batch_id
                and prepared.status == "ok"
                and prepared.sent_state == SENT_STATE_SETTLED
            ):
                batches = batches_from_entries(entries, session_id=session_id, run_id=run_id)
                delivered = delivered_entry(
                    session_id,
                    run_id,
                    batch_id=state.pending_delivery_batch_id,
                    provider_effect_id=prepared.effect_id,
                    batches=batches,
                )
                if delivered is not None:
                    rows.append(delivered)
            rows.append(
                operation_state_entry(
                    mark_provider_effect_settled(state, effect_id=prepared.effect_id)
                )
            )
            committed = prepared
            return tuple(rows)

        self.session_log.mutate(session_id, mutation)
        if committed is None:
            raise RuntimeEffectError("provider effect settlement was not committed")
        return committed

    def begin_tool_batch(
        self,
        session_id: str,
        run_id: str,
        *,
        intents: Iterable[RuntimeEffectIntent],
        delivery_intent: DeliveryBatchIntent,
        driver: str = "",
    ) -> ToolBatchCommit:
        committed: ToolBatchCommit | None = None

        def mutation(projection, entries):
            nonlocal committed
            state = _require_open_state(
                projection,
                entries,
                session_id=session_id,
                run_id=run_id,
            )
            effect_driver = _driver_for_state(state, explicit=driver)
            existing_effects = effects_from_entries(
                entries,
                session_id=session_id,
                run_id=run_id,
            )
            prepared_intents = tuple(
                prepare_intent(session_id, run_id, intent) for intent in intents
            )
            for intent in prepared_intents:
                _require_new_effect_id(existing_effects, intent.effect_id)
            prepared_delivery = prepare_batch_intent(session_id, run_id, delivery_intent)
            batches = batches_from_entries(entries, session_id=session_id, run_id=run_id)
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
                    effect_ids=effect_ids,
                    driver=effect_driver,
                    delivery_batch_id=prepared_delivery.batch_id,
                    turn=prepared_delivery.turn,
                )
            else:
                next_state = mark_tool_delivery_pending(
                    state,
                    driver=effect_driver,
                    delivery_batch_id=prepared_delivery.batch_id,
                    turn=prepared_delivery.turn,
                )
            rows.append(operation_state_entry(next_state))
            committed = ToolBatchCommit(
                batch_id=prepared_delivery.batch_id,
                effect_ids=effect_ids,
                driver=effect_driver,
            )
            return tuple(rows)

        self.session_log.mutate(session_id, mutation)
        if committed is None:
            raise RuntimeEffectError("tool batch was not committed")
        return committed

    def settle_tool_effect(
        self,
        session_id: str,
        run_id: str,
        settlement: RuntimeEffectSettlement,
    ) -> RuntimeEffectSettlement:
        committed: RuntimeEffectSettlement | None = None

        def mutation(projection, entries):
            nonlocal committed
            state = _require_open_state(
                projection,
                entries,
                session_id=session_id,
                run_id=run_id,
            )
            effects = effects_from_entries(entries, session_id=session_id, run_id=run_id)
            matching = _find_effect(effects, settlement.effect_id)
            prepared = prepare_settlement(session_id, run_id, settlement, effects)
            if matching.settlement is not None:
                committed = prepared
                return ()
            rows = [effect_settlement_entry(prepared)]
            if prepared.effect_id in state.pending_effect_ids:
                rows.append(
                    operation_state_entry(
                        mark_tool_effect_settled(state, effect_id=prepared.effect_id)
                    )
                )
            committed = prepared
            return tuple(rows)

        self.session_log.mutate(session_id, mutation)
        if committed is None:
            raise RuntimeEffectError("tool effect settlement was not committed")
        return committed

    def settle_effect(
        self,
        session_id: str,
        run_id: str,
        settlement: RuntimeEffectSettlement,
    ) -> RuntimeEffectSettlement:
        if settlement.effect_category == EFFECT_CATEGORY_PROVIDER_SEND:
            return self.settle_provider_effect(session_id, run_id, settlement)
        if settlement.effect_category == EFFECT_CATEGORY_TOOL_CALL:
            return self.settle_tool_effect(session_id, run_id, settlement)
        raise RuntimeEffectError(f"unsupported effect category: {settlement.effect_category}")

    def record_delivery_recovered(
        self,
        session_id: str,
        run_id: str,
        *,
        batch_id: str,
        recovered_effect_ids: Iterable[str],
        recovered_reads: int = 0,
        recovered_lookups: int = 0,
    ) -> None:
        recovered_ids = tuple(recovered_effect_ids)

        def mutation(projection, entries):
            state = _require_open_state(
                projection,
                entries,
                session_id=session_id,
                run_id=run_id,
            )
            if (
                state.leaf != LEAF_TOOL_DELIVERY_PENDING
                or state.pending_delivery_batch_id != batch_id
            ):
                raise RuntimeOperationTransitionError(
                    "delivery recovery requires matching delivery_pending state"
                )
            batches = batches_from_entries(entries, session_id=session_id, run_id=run_id)
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

        self.session_log.mutate(session_id, mutation)


def _operation_settled_entry(state: RuntimeOperationState) -> dict[str, object]:
    return {
        "lane": state.lane,
        "operation_id": state.operation_id,
        "kind": "operation_settled",
        "payload": outcome_for_terminal(state).to_payload(),
    }


def _require_state(
    entries: tuple[RuntimeLogEntry, ...],
    *,
    session_id: str,
    run_id: str,
) -> RuntimeOperationState:
    state = operation_state_from_entries(
        entries,
        session_id=session_id,
        run_id=run_id,
    )
    if state is None:
        raise RuntimeOperationTransitionError("operation state is missing")
    return state


def _require_open_state(
    projection,
    entries: tuple[RuntimeLogEntry, ...],
    *,
    session_id: str,
    run_id: str,
) -> RuntimeOperationState:
    state = _require_state(entries, session_id=session_id, run_id=run_id)
    operation_is_open(projection, state)
    return state


def _driver_for_state(state: RuntimeOperationState, *, explicit: str = "") -> str:
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


def _require_new_effect_id(
    effects: tuple[RuntimeEffectProjection, ...],
    effect_id: str,
) -> None:
    if any(effect.intent.effect_id == effect_id for effect in effects):
        raise RuntimeEffectError(f"duplicate effect id: {effect_id}")


def _find_effect(
    effects: tuple[RuntimeEffectProjection, ...],
    effect_id: str,
) -> RuntimeEffectProjection:
    found = next((effect for effect in effects if effect.intent.effect_id == effect_id), None)
    if found is None:
        raise RuntimeEffectError(f"effect intent not found: {effect_id}")
    return found


__all__ = [
    "RuntimeMutationLine",
    "ToolBatchCommit",
]
