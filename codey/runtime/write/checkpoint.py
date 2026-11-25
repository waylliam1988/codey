"""Read runtime checkpoints and apply typed transitions in one durable transaction."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, TypeAlias

from codey.durable.errors import CommitConflict
from codey.durable.model import (
    ArtifactRecord,
    DurableMutation,
    EffectRecord,
    EntryDraft,
    SubmissionRecord,
    TaskCheckpoint,
    TaskStatus,
    thaw_json,
)
from codey.durable.reducer import with_effect_intents, with_effect_settlement
from codey.runtime.core.operation_payload import RuntimeOperationState, RuntimeOperationTransitionError
from codey.runtime.core.operation_state import lane_for_run, operation_id_for_run
from codey.runtime.effects.effect_records import (
    RuntimeEffectError,
    RuntimeEffectIntent,
    RuntimeEffectSettlement,
    project_durable_effects,
)
from codey.runtime.effects.tool_result_delivery import DeliveryBatchProjection, delivery_from_checkpoint
from codey.runtime.log.session_view import SessionView

if TYPE_CHECKING:
    from codey.durable.contracts import RuntimeCheckpointPort

RuntimeChange: TypeAlias = (
    RuntimeOperationState | RuntimeEffectIntent | RuntimeEffectSettlement | DeliveryBatchProjection
)
RuntimeChanges: TypeAlias = tuple[RuntimeChange, ...]
RuntimeChangesBuilder: TypeAlias = Callable[[SessionView], RuntimeChanges]


class RuntimeOperationStore:
    """Read projection for one run's durable operation state."""

    def __init__(self, *, durable: RuntimeCheckpointPort) -> None:
        if durable is None:
            raise ValueError("an operation store requires durable storage")
        self.durable = durable

    def load(self, session_id: str, run_id: str) -> RuntimeOperationState | None:
        try:
            task = self.durable.task(run_id)
        except KeyError:
            return None
        return operation_from_checkpoint(task, session_id)


def operation_from_checkpoint(task: TaskCheckpoint, session_id: str) -> RuntimeOperationState | None:
    if task.conversation_id != session_id:
        raise RuntimeOperationTransitionError("checkpoint conversation boundary mismatch")
    payload = thaw_json(task.state.get("operation"))
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise RuntimeOperationTransitionError("invalid durable operation checkpoint")
    if "task_policy" in task.state:
        payload["task_policy"] = thaw_json(task.state["task_policy"])
    state = RuntimeOperationState.from_payload(payload)
    if (
        state is None or state.run_id != task.task_id or state.session_id != session_id
        or state.lane != lane_for_run(task.task_id) or state.operation_id != operation_id_for_run(task.task_id)
    ):
        raise RuntimeOperationTransitionError("invalid durable operation checkpoint")
    return state


def delivery_snapshot(batch: DeliveryBatchProjection) -> dict[str, object]:
    intent = batch.intent.to_payload()
    return {
        "intent": intent, "send_attempts": list(batch.send_attempts),
        "superseded_effect_ids": list(batch.superseded_effect_ids),
        "delivered_effect_ids": list(batch.delivered_effect_ids),
        "is_recovered": batch.is_recovered, "recovered_effect_ids": list(batch.recovered_effect_ids),
        "recovered_reads": batch.recovered_reads, "recovered_lookups": batch.recovered_lookups,
        "is_abandoned": batch.is_abandoned, "abandoned_reason": batch.abandoned_reason,
    }


def view_from_checkpoint(task: TaskCheckpoint, effects: tuple[EffectRecord, ...], *, session_id: str) -> SessionView:
    state = operation_from_checkpoint(task, session_id)
    if state is None:
        return SessionView(None, (), ())
    projections = project_durable_effects(effects, session_id=session_id, run_id=task.task_id)
    return SessionView(state, projections, delivery_from_checkpoint(task, session_id=session_id))


def _apply_boundary_transition(mutation: DurableMutation, state: RuntimeOperationState) -> DurableMutation:
    if mutation.task is None:
        raise RuntimeOperationTransitionError("boundary lost its task")
    payload = state.to_payload()
    payload.pop("task_policy", None)
    return replace(mutation, task=replace(mutation.task, state={**mutation.task.state, "operation": payload}))


def _apply_changes(
    task: TaskCheckpoint,
    view: SessionView,
    effects: tuple[EffectRecord, ...],
    changes: RuntimeChanges,
) -> tuple[RuntimeOperationState, tuple[DeliveryBatchProjection, ...], list[EffectRecord], list[EffectRecord]]:
    state = view.state
    if state is not None and state.leaf == "terminal":
        raise RuntimeOperationTransitionError("terminal operation is immutable")
    batches = {batch.intent.batch_id: batch for batch in view.batches}
    known = {effect.effect_id: effect for effect in effects}
    intents: dict[str, EffectRecord] = {}
    settlements: dict[str, EffectRecord] = {}
    for change in changes:
        if isinstance(change, RuntimeOperationState):
            state = change
        elif isinstance(change, DeliveryBatchProjection):
            batches[change.intent.batch_id] = change
        elif isinstance(change, RuntimeEffectIntent):
            if change.effect_id in known or change.effect_id in intents:
                raise RuntimeEffectError(f"duplicate effect intent: {change.effect_id}")
            intents[change.effect_id] = EffectRecord(
                change.effect_id, change.run_id, change.effect_category, change.replay_class,
                change.args_digest, "pending", change.to_payload())
        else:
            prior = known.get(change.effect_id)
            if prior is None:
                raise RuntimeEffectError("effect must commit intent before settlement")
            if prepared := settlements.get(change.effect_id):
                prior = prepared
            updated = replace(prior, settlement=change.to_payload(), status=change.status)
            if prior.settlement is None:
                settlements[change.effect_id] = updated
            elif prior.settlement != updated.settlement:
                raise RuntimeEffectError("effect outcome is immutable")
    if (state is None or state.run_id != task.task_id or state.session_id != task.conversation_id
            or RuntimeOperationState.from_payload(state.to_payload()) is None):
        raise RuntimeOperationTransitionError("invalid runtime mutation operation state")
    return state, tuple(batches.values()), list(intents.values()), list(settlements.values())


def mutate_checkpoint(durable: RuntimeCheckpointPort, session_id: str, run_id: str,
                      derive_changes: RuntimeChangesBuilder, *,
                      messages: tuple[tuple[str, Mapping[str, object]], ...] = (),
                      artifacts: tuple[ArtifactRecord, ...] = (),
                      task_updates: tuple[TaskCheckpoint, ...] = (),
                      generation_boundary: str | None = None,
                      boundary_result: list[SubmissionRecord] | None = None,
                      boundary_transition: Callable[[RuntimeOperationState, tuple[SubmissionRecord, ...]], RuntimeOperationState] | None = None) -> None:
    def derive(task: TaskCheckpoint, effects: tuple[EffectRecord, ...], revision: int) -> DurableMutation | None:
        if task.task_id != run_id:
            raise RuntimeOperationTransitionError("checkpoint task boundary mismatch")
        view = view_from_checkpoint(task, effects, session_id=session_id)
        changes = derive_changes(view)
        if not changes:
            return None
        state, batches, intents, settlements = _apply_changes(task, view, effects, changes)
        next_task = replace(task, revision=task.revision + 1)
        if intents:
            if task.status != TaskStatus.RUNNING:
                raise CommitConflict("runtime effects require a running task")
            next_task = with_effect_intents(task, tuple(intents))
        for settlement in settlements:
            next_task = with_effect_settlement(next_task, settlement)
        operation_payload = state.to_payload()
        operation_payload.pop("task_policy", None)
        policy = {"task_policy": state.task_policy} if state.task_policy else {}
        next_task = replace(next_task, revision=task.revision + 1, state={
            **next_task.state, **policy, "operation": operation_payload,
            "delivery_batches": [delivery_snapshot(batch) for batch in batches],
        })
        head, _revision = durable.head(session_id)
        appended: list[EntryDraft] = []
        for kind, payload in messages:
            entry = EntryDraft(f"entry-{uuid.uuid4().hex}", session_id, head, kind, payload, by_task_id=run_id)
            appended.append(entry)
            head = entry.entry_id
        mutation = DurableMutation(
            session_id, revision, task=next_task, task_updates=task_updates, entries=tuple(appended),
            effects=tuple(intents), settlements=tuple(settlements), artifacts=_unregistered_artifacts(durable, artifacts),
        )
        if generation_boundary is not None:
            selected, mutation = durable.merge_generation_boundary(
                mutation, task_id=run_id, at=generation_boundary,
            )
            if boundary_transition is not None:
                mutation = _apply_boundary_transition(mutation, boundary_transition(state, selected))
            if boundary_result is not None:
                boundary_result[:] = selected
        return mutation

    # A user stop may advance the task checkpoint while the provider worker is
    # settling its runtime operation.  Rebuild the mutation from the newer
    # checkpoint rather than turning that expected cancellation race into an
    # unhandled worker failure.  The reducer is pure and the durable CAS still
    # rejects any competing write after the bounded retry budget.
    for attempt in range(3):
        try:
            durable.transact_task(run_id, derive)
            return
        except CommitConflict:
            if attempt == 2:
                raise


def _unregistered_artifacts(durable: RuntimeCheckpointPort, artifacts: tuple[ArtifactRecord, ...]) -> tuple[ArtifactRecord, ...]:
    """Reuse immutable content references inside the same transaction read line."""
    added = []
    for artifact in artifacts:
        try:
            existing = durable.artifact(artifact.handle)
        except KeyError:
            added.append(artifact)
            continue
        if replace(artifact, created_at=existing.created_at) != existing:
            raise CommitConflict("artifact handle has conflicting content or ownership")
    return tuple(added)
