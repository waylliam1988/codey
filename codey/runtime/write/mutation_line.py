"""Serialized runtime mutation boundary.

Every production mutation of durable operation state, effect ledger, delivery
ledger, and operation settlement goes through this line. The callback passed to
``RuntimeSessionLog.mutate`` stays pure: it reads the current projection and
entries, decides the next bounded records, and commits them as one batch.
"""

from __future__ import annotations

from typing import Callable, Iterable

from codey.runtime.effects.effect_records import (
    EFFECT_CATEGORY_PROVIDER_SEND,
    EFFECT_CATEGORY_TOOL_CALL,
    RuntimeEffectError,
    RuntimeEffectIntent,
    RuntimeEffectSettlement,
)
from codey.runtime.core.operation_state import (
    LEAF_TERMINAL,
    RuntimeOperationState,
    RuntimeOperationTransitionError,
    mark_completion_blocked as state_mark_completion_blocked,
    mark_completion_proof_recorded as state_mark_completion_proof_recorded,
    mark_repair_context_admitted as state_mark_repair_context_admitted,
    mark_repair_running as state_mark_repair_running,
    mark_repair_settled as state_mark_repair_settled,
    mark_terminal as state_mark_terminal,
    mark_writer_running as state_mark_writer_running,
    mark_writer_settled as state_mark_writer_settled,
    new_operation_state,
    operation_is_open,
    operation_state_entry,
    operation_state_from_entries,
    outcome_for_terminal,
    start_entries,
)
from codey.runtime.log.session_log import RuntimeLogEntry, RuntimeSessionLog
from codey.runtime.log.session_view import load_session_view
from codey.runtime.effects.tool_result_delivery import DeliveryBatchIntent
from codey.runtime.write.delivery_recovery import build_delivery_recovered_rows
from codey.runtime.write.provider_effects import (
    build_provider_begin_rows,
    build_provider_settle_rows,
)
from codey.runtime.write.tool_batches import (
    ToolBatchCommit,
    build_tool_batch_rows,
    build_tool_settle_rows,
)


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
    ) -> RuntimeOperationState | None:
        return self._commit_state(
            session_id,
            run_id,
            lambda state: state_mark_completion_proof_recorded(
                state,
                proof_ref=proof_ref,
                proof_status=proof_status,
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
            view = load_session_view(entries, session_id=session_id, run_id=run_id)
            rows, prepared = build_provider_begin_rows(
                projection,
                view,
                session_id=session_id,
                run_id=run_id,
                intent=intent,
                driver=driver,
                delivery_batch_id=delivery_batch_id,
            )
            committed = prepared
            return rows

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
            view = load_session_view(entries, session_id=session_id, run_id=run_id)
            rows, prepared = build_provider_settle_rows(
                projection,
                view,
                session_id=session_id,
                run_id=run_id,
                settlement=settlement,
            )
            committed = prepared
            return rows

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
            view = load_session_view(entries, session_id=session_id, run_id=run_id)
            rows, batch_commit = build_tool_batch_rows(
                projection,
                view,
                session_id=session_id,
                run_id=run_id,
                intents=intents,
                delivery_intent=delivery_intent,
                driver=driver,
            )
            committed = batch_commit
            return rows

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
            view = load_session_view(entries, session_id=session_id, run_id=run_id)
            rows, prepared = build_tool_settle_rows(
                projection,
                view,
                session_id=session_id,
                run_id=run_id,
                settlement=settlement,
            )
            committed = prepared
            return rows

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
            view = load_session_view(entries, session_id=session_id, run_id=run_id)
            return build_delivery_recovered_rows(
                projection,
                view,
                session_id=session_id,
                run_id=run_id,
                batch_id=batch_id,
                recovered_ids=recovered_ids,
                recovered_reads=recovered_reads,
                recovered_lookups=recovered_lookups,
            )

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


__all__ = [
    "RuntimeMutationLine",
    "ToolBatchCommit",
]
