"""Serialized runtime mutation boundary.

Every production mutation of durable operation state, effect ledger, delivery
ledger, and operation settlement goes through the durable checkpoint adapter.
Audit projections are read separately and cannot be selected as a write backend.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from codey.durable.model import ArtifactRecord

from codey.runtime.core.operation_payload import RuntimeExecutionCounters
from codey.runtime.core.operation_reducer import prepare_operation_settlement, record_progress_observation
from codey.runtime.core.operation_state import (
    LEAF_TERMINAL,
    RuntimeOperationState,
    RuntimeOperationTransitionError,
    new_operation_state,
    operation_is_open,
    operation_state_entry,
    operation_state_from_entries,
    outcome_for_terminal,
    start_entries,
)
from codey.runtime.core.operation_state import (
    mark_candidate_validation_running as state_mark_candidate_validation_running,
)
from codey.runtime.core.operation_state import (
    mark_completion_blocked as state_mark_completion_blocked,
)
from codey.runtime.core.operation_state import (
    mark_completion_proof_recorded as state_mark_completion_proof_recorded,
)
from codey.runtime.core.operation_state import (
    mark_repair_context_admitted as state_mark_repair_context_admitted,
)
from codey.runtime.core.operation_state import (
    mark_repair_running as state_mark_repair_running,
)
from codey.runtime.core.operation_state import (
    mark_repair_settled as state_mark_repair_settled,
)
from codey.runtime.core.operation_state import (
    mark_terminal as state_mark_terminal,
)
from codey.runtime.core.operation_state import (
    mark_tool_delivery_settled as state_mark_tool_delivery_settled,
)
from codey.runtime.core.operation_state import (
    mark_writer_running as state_mark_writer_running,
)
from codey.runtime.core.operation_state import (
    mark_writer_settled as state_mark_writer_settled,
)
from codey.runtime.core.progress import ProgressSnapshot
from codey.runtime.effects.effect_records import (
    EFFECT_CATEGORY_PROVIDER_SEND,
    EFFECT_CATEGORY_TOOL_CALL,
    RuntimeEffectError,
    RuntimeEffectIntent,
    RuntimeEffectSettlement,
)
from codey.runtime.effects.tool_result_delivery import DeliveryBatchIntent, abandoned_entry
from codey.runtime.log.session_view import load_session_view
from codey.runtime.write.delivery_recovery import build_delivery_recovered_rows
from codey.runtime.write.provider_effects import (
    ProviderAdmission,
    build_provider_begin_rows,
    build_provider_prepare_rows,
    build_provider_settle_rows,
)
from codey.runtime.write.tool_batches import (
    ToolBatchCommit,
    build_tool_batch_rows,
    build_tool_settle_rows,
)


class RuntimeMutationLine:
    """Single writer-facing API for runtime durable mutations."""
    def __init__(self, *, durable: Any) -> None:
        if durable is None:
            raise ValueError("a runtime mutation line requires durable storage")
        self.durable = durable

    def advance_execution(
        self, session_id: str, run_id: str, *, expected: RuntimeOperationState,
        leaf: str, artifact: ArtifactRecord | None = None,
        logical_turn: int | None = None, counters: RuntimeExecutionCounters | None = None,
    ) -> None:
        from codey.runtime.core.operation_state import advance_execution

        def mutation(projection: Any, entries: Any) -> Any:
            view = load_session_view(entries, session_id=session_id, run_id=run_id)
            current = view.state
            if current is None:
                raise RuntimeEffectError("execution requires an admitted operation")
            work_ref = artifact.handle if artifact is not None else None
            proposed = advance_execution(expected, leaf, logical_turn=logical_turn,
                                         work_ref=work_ref, counters=counters)
            if current == proposed:
                return ()
            if current != expected:
                raise RuntimeEffectError("execution action has a stale operation identity")
            operation_is_open(projection, current)
            return (operation_state_entry(proposed),)

        self._mutate(session_id, run_id, mutation, artifacts=(artifact,) if artifact is not None else ())

    def _mutate(self, session_id: str, run_id: str, mutation: Callable[..., Any], *,
                messages: tuple[tuple[str, Mapping[str, object]], ...] = (),
                artifacts: tuple[ArtifactRecord, ...] = ()) -> None:
        checkpoint = importlib.import_module("codey.durable.checkpoint")
        checkpoint.mutate_checkpoint(
            self.durable, session_id, run_id, mutation, messages=messages, artifacts=artifacts,
        )

    def admit_reviewer_selection(self, session_id: str, run_id: str, payload: dict[str, object]) -> RuntimeOperationState | None:
        from codey.runtime.core.operation_payload import _parse_model_selection

        admitted = _parse_model_selection(payload)

        def transition(state: RuntimeOperationState) -> RuntimeOperationState:
            if state.reviewer_selection and state.reviewer_selection != admitted:
                raise RuntimeOperationTransitionError("admitted reviewer selection is immutable")
            return replace(state, reviewer_selection=admitted)

        return self._commit_state(session_id, run_id, transition)


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
        model_selection: dict[str, object] | None = None,
    ) -> RuntimeOperationState | None:
        accepted: RuntimeOperationState | None = None

        def mutation(projection: Any, entries: Any) -> Any:
            nonlocal accepted
            existing = operation_state_from_entries(
                entries,
                session_id=session_id,
                run_id=run_id,
            )
            if existing is not None:
                if model_selection is not None and existing.model_selection != model_selection:
                    raise RuntimeOperationTransitionError("admitted API selection is immutable")
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
                model_selection=model_selection,
            )
            accepted = state
            return start_entries(projection, state)

        self._mutate(session_id, run_id, mutation)
        return accepted

    def _commit_state(
        self,
        session_id: str,
        run_id: str,
        transition: Callable[[RuntimeOperationState], RuntimeOperationState],
        *,
        abandoned_reason: str = "",
    ) -> RuntimeOperationState | None:
        committed: RuntimeOperationState | None = None

        def mutation(projection: Any, entries: Any) -> Any:
            nonlocal committed
            view = load_session_view(entries, session_id=session_id, run_id=run_id)
            current = view.state
            if current is None:
                raise RuntimeOperationTransitionError("operation state is missing")
            prepared = prepare_operation_settlement(view) if abandoned_reason else None
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
            rows: list[dict[str, object]] = []
            if prepared is not None:
                for batch_id in prepared.abandoned_batch_ids:
                    entry = abandoned_entry(session_id, run_id, batch_id=batch_id,
                                            reason=abandoned_reason, batches=view.batches)
                    if entry is not None:
                        rows.append(entry)
                if prepared.abandoned_batch_ids and current.leaf == "tool_delivery_pending":
                    current = state_mark_tool_delivery_settled(current)
            next_state = transition(current)
            if next_state == current and not rows:
                committed = current
                return ()
            rows.append(operation_state_entry(next_state))
            if next_state.leaf == LEAF_TERMINAL:
                rows.append(_operation_settled_entry(next_state))
            committed = next_state
            return tuple(rows)

        self._mutate(session_id, run_id, mutation)
        return committed

    def mark_writer_running(
        self,
        session_id: str,
        run_id: str,
        *,
        provider_id: str,
        writer_attempt: int | None = None,
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

    def mark_candidate_validation_running(
        self, session_id: str, run_id: str, *, provider_id: str, writer_attempt: int,
    ) -> RuntimeOperationState | None:
        return self._commit_state(session_id, run_id,
            lambda state: state_mark_candidate_validation_running(state, provider_id=provider_id,
                                                                  writer_attempt=writer_attempt))

    def set_task_policy(
        self,
        session_id: str,
        run_id: str,
        *,
        policy: dict[str, object],
    ) -> RuntimeOperationState | None:
        """Persist the entry authorization snapshot on the operation state."""
        from codey.runtime.core.operation_payload import _parse_task_policy

        normalized = _parse_task_policy(policy)
        return self._commit_state(
            session_id,
            run_id,
            lambda state: _replace_task_policy(state, normalized),
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
            abandoned_reason="writer_settled",
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
            abandoned_reason="repair_settled",
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
        final_delivery: str | None = None,
        proof_ref: str = "",
        proof_status: str = "",
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
                final_delivery=final_delivery,
                proof_ref=proof_ref,
                proof_status=proof_status,
            ),
            abandoned_reason="operation_terminal",
        )

    def prepare_provider_request(
        self, session_id: str, run_id: str, *, expected: RuntimeOperationState,
        artifact: ArtifactRecord, turn: int, logical_turn: int | None = None,
    ) -> None:
        if (artifact.owner_task_id != run_id or artifact.owner_effect_id is not None
                or artifact.metadata.get("kind") != "provider_request"):
            raise RuntimeEffectError("prepared provider request requires a task-owned artifact")

        def mutation(projection: Any, entries: Any) -> Any:
            view = load_session_view(entries, session_id=session_id, run_id=run_id)
            return build_provider_prepare_rows(projection, view, expected=expected,
                                                request_ref=artifact.handle, turn=turn, logical_turn=logical_turn)

        self._mutate(session_id, run_id, mutation, artifacts=(artifact,))

    def begin_provider_effect(
        self,
        session_id: str,
        run_id: str,
        intent: RuntimeEffectIntent,
        *,
        driver: str = "",
        delivery_batch_id: str = "",
        supersede_effect_id: str = "",
        artifacts: tuple[ArtifactRecord, ...] = (),
        counters: RuntimeExecutionCounters | None = None,
        expected_counters: RuntimeExecutionCounters | None = None,
    ) -> RuntimeEffectIntent:
        return self._admit_provider_effect(session_id, run_id, intent, driver=driver,
            delivery_batch_id=delivery_batch_id, supersede_effect_id=supersede_effect_id,
            artifacts=artifacts, counters=counters, expected_counters=expected_counters).intent

    def admit_provider_request(
        self, session_id: str, run_id: str, intent: RuntimeEffectIntent, *,
        expected_counters: RuntimeExecutionCounters, counters: RuntimeExecutionCounters,
    ) -> ProviderAdmission:
        if not intent.request_ref:
            raise RuntimeEffectError("provider request admission requires its frozen reference")
        return self._admit_provider_effect(session_id, run_id, intent,
            delivery_batch_id=intent.delivery_batch_id, expected_request_ref=intent.request_ref,
            expected_counters=expected_counters, counters=counters)

    def _admit_provider_effect(
        self, session_id: str, run_id: str, intent: RuntimeEffectIntent, *,
        driver: str = "", delivery_batch_id: str = "", supersede_effect_id: str = "",
        artifacts: tuple[ArtifactRecord, ...] = (),
        counters: RuntimeExecutionCounters | None = None,
        expected_counters: RuntimeExecutionCounters | None = None,
        expected_request_ref: str = "",
    ) -> ProviderAdmission:
        committed: ProviderAdmission | None = None

        def mutation(projection: Any, entries: Any) -> Any:
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
                supersede_effect_id=supersede_effect_id,
                counters=counters,
                expected_counters=expected_counters,
                expected_request_ref=expected_request_ref,
            )
            committed = ProviderAdmission(prepared, bool(rows))
            return rows

        self._mutate(session_id, run_id, mutation, artifacts=artifacts)
        if committed is None:
            raise RuntimeEffectError("provider effect intent was not committed")
        return committed

    def record_progress(
        self, session_id: str, run_id: str, *, batch_id: str,
        expected: ProgressSnapshot, observed: ProgressSnapshot,
    ) -> None:
        def mutation(projection: Any, entries: Any) -> Any:
            view = load_session_view(entries, session_id=session_id, run_id=run_id)
            return record_progress_observation(view, batch_id=batch_id, expected=expected, observed=observed)

        self._mutate(session_id, run_id, mutation)

    def record_provider_response(
        self, session_id: str, run_id: str, *, effect_id: str, artifact: ArtifactRecord,
        messages: tuple[tuple[str, Mapping[str, object]], ...] = (),
    ) -> None:
        """Admit a received body without settling input/history generation boundaries."""
        from codey.runtime.core.operation_state import mark_provider_response_ready, operation_state_entry
        from codey.runtime.effects.effect_records import find_effect
        from codey.runtime.log.session_view import require_open_view

        def mutation(projection: Any, entries: Any) -> Any:
            view = load_session_view(entries, session_id=session_id, run_id=run_id)
            state = require_open_view(projection, view)
            effect = find_effect(view.effects, effect_id)
            if (effect.intent.effect_category != "provider_send" or effect.settlement is not None
                    or artifact.owner_task_id != run_id or artifact.owner_effect_id != effect_id
                    or artifact.metadata.get("kind") != "provider_response"):
                raise RuntimeEffectError("provider response does not match its pending effect")
            if state.response_ref == artifact.handle:
                return ()
            if state.turn != effect.intent.turn:
                raise RuntimeEffectError("provider response does not match its operation turn")
            return (operation_state_entry(mark_provider_response_ready(state, response_ref=artifact.handle)),)

        self._mutate(session_id, run_id, mutation, artifacts=(artifact,), messages=messages)

    def settle_provider_effect(
        self,
        session_id: str,
        run_id: str,
        settlement: RuntimeEffectSettlement,
        *,
        messages: tuple[tuple[str, Mapping[str, object]], ...] = (),
    ) -> RuntimeEffectSettlement:
        committed, _placed = self._settle_provider_effect(
            session_id, run_id, settlement, messages=messages,
        )
        return committed

    def settle_provider_effect_at_boundary(
        self,
        session_id: str,
        run_id: str,
        settlement: RuntimeEffectSettlement,
        *,
        boundary: str,
        messages: tuple[tuple[str, Mapping[str, object]], ...] = (),
    ) -> tuple[object, ...]:
        """Settle a provider generation and place its inbox in one commit."""
        _committed, placed = self._settle_provider_effect(
            session_id, run_id, settlement, messages=messages,
            generation_boundary=boundary,
        )
        return placed

    def settle_kernel_boundary(
        self, session_id: str, run_id: str, *, expected: RuntimeOperationState,
        settlement: RuntimeEffectSettlement | None, boundary: str,
    ) -> tuple[object, ...]:
        """Commit the last generation, final input placement and kernel verdict."""
        from codey.runtime.core.operation_state import (
            LEAF_KERNEL_SETTLED,
            LEAF_REPAIR_RUNNING,
            LEAF_WRITER_RUNNING,
            advance_execution,
        )

        def mutation(projection: Any, entries: Any) -> Any:
            view = load_session_view(entries, session_id=session_id, run_id=run_id)
            if view.state != expected:
                raise RuntimeEffectError("kernel final boundary has a stale operation identity")
            rows: tuple[dict[str, object], ...] = ()
            state = expected
            if settlement is not None:
                rows, _ = build_provider_settle_rows(projection, view, session_id=session_id,
                                                     run_id=run_id, settlement=settlement)
            return (*rows, operation_state_entry(advance_execution(state, LEAF_KERNEL_SETTLED)))

        def placed_transition(state: RuntimeOperationState, selected: tuple[object, ...]) -> RuntimeOperationState:
            if boundary != "final" or not selected:
                return state
            # New requirements invalidate the old completion proposal. Their
            # placement and reopening share this same durable transaction.
            return advance_execution(state, LEAF_REPAIR_RUNNING if state.driver == "repair" else LEAF_WRITER_RUNNING)

        placed: list[object] = []
        checkpoint = importlib.import_module("codey.durable.checkpoint")
        checkpoint.mutate_checkpoint(self.durable, session_id, run_id, mutation,
            generation_boundary=boundary, boundary_result=placed, boundary_transition=placed_transition)
        return tuple(placed)

    def _settle_provider_effect(
        self,
        session_id: str,
        run_id: str,
        settlement: RuntimeEffectSettlement,
        *,
        messages: tuple[tuple[str, Mapping[str, object]], ...] = (),
        generation_boundary: str | None = None,
    ) -> tuple[RuntimeEffectSettlement, tuple[object, ...]]:
        committed: RuntimeEffectSettlement | None = None
        placed: list[object] = []

        def mutation(projection: Any, entries: Any) -> Any:
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

        checkpoint = importlib.import_module("codey.durable.checkpoint")
        checkpoint.mutate_checkpoint(
            self.durable, session_id, run_id, mutation, messages=messages,
            generation_boundary=generation_boundary, boundary_result=placed,
        )
        if committed is None:
            raise RuntimeEffectError("provider effect settlement was not committed")
        return committed, tuple(placed)

    def begin_tool_batch(
        self,
        session_id: str,
        run_id: str,
        *,
        intents: Iterable[RuntimeEffectIntent],
        delivery_intent: DeliveryBatchIntent | None,
        driver: str = "",
    ) -> ToolBatchCommit:
        committed: ToolBatchCommit | None = None

        def mutation(projection: Any, entries: Any) -> Any:
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

        self._mutate(session_id, run_id, mutation)
        if committed is None:
            raise RuntimeEffectError("tool batch was not committed")
        return committed

    def settle_tool_effect(
        self,
        session_id: str,
        run_id: str,
        settlement: RuntimeEffectSettlement,
        *,
        messages: tuple[tuple[str, Mapping[str, object]], ...] = (),
        artifacts: tuple[ArtifactRecord, ...] = (),
    ) -> RuntimeEffectSettlement:
        committed: RuntimeEffectSettlement | None = None

        def mutation(projection: Any, entries: Any) -> Any:
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

        self._mutate(session_id, run_id, mutation, messages=messages, artifacts=tuple(artifacts))
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

        def mutation(projection: Any, entries: Any) -> Any:
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

        self._mutate(session_id, run_id, mutation)

    def record_delivery_delivered(
        self,
        session_id: str,
        run_id: str,
        *,
        batch_id: str,
        provider_effect_id: str,
    ) -> None:
        def mutation(projection: Any, entries: Any) -> Any:
            view = load_session_view(entries, session_id=session_id, run_id=run_id)
            from codey.runtime.write.provider_effects import build_delivery_delivered_rows

            rows = build_delivery_delivered_rows(
                projection, view, session_id=session_id, run_id=run_id,
                batch_id=batch_id, provider_effect_id=provider_effect_id,
            )
            return rows

        self._mutate(session_id, run_id, mutation)


def _operation_settled_entry(state: RuntimeOperationState) -> dict[str, object]:
    return {
        "lane": state.lane,
        "operation_id": state.operation_id,
        "kind": "operation_settled",
        "payload": outcome_for_terminal(state).to_payload(),
    }


def _replace_task_policy(state: RuntimeOperationState, policy: dict[str, object]) -> RuntimeOperationState:
    if state.task_policy == policy:
        return state
    if state.task_policy:
        raise RuntimeOperationTransitionError("task authorization snapshot is immutable")
    from dataclasses import replace

    return replace(state, task_policy=dict(policy))


__all__ = [
    "RuntimeOperationState",
    "RuntimeMutationLine",
    "ToolBatchCommit",
]
