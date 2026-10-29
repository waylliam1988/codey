"""Translate pure runtime reducer output into one durable task mutation.

Only current operation and delivery snapshots are stored in the checkpoint.
Effect identities and outcomes live exclusively in the effects table. The
bounded row envelope is an in-memory adapter for existing pure validators;
no runtime event history or JSONL is loaded to make a business decision.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

from codey.durable.model import ArtifactRecord, DurableMutation, EffectRecord, EntryDraft, TaskCheckpoint, thaw_json
from codey.durable.reducer import with_effect_intents, with_effect_settlement

if TYPE_CHECKING:
    from codey.durable.session import DurableSession
    from codey.runtime.core.operation_state import RuntimeOperationState
    from codey.runtime.effects.tool_result_delivery import DeliveryBatchProjection
    from codey.runtime.log.entries import RuntimeLogEntry


def _entry(session_id: str, row: dict[str, Any]) -> RuntimeLogEntry:
    from codey.runtime.log.entries import RuntimeLogEntry

    return RuntimeLogEntry(session_id=session_id, lane=row["lane"], operation_id=row["operation_id"],
                           kind=row["kind"], payload=row["payload"])


def operation_from_checkpoint(task: TaskCheckpoint, session_id: str) -> RuntimeOperationState | None:
    from codey.runtime.core.operation_state import RuntimeOperationState, RuntimeOperationTransitionError

    if task.conversation_id != session_id:
        raise RuntimeOperationTransitionError("checkpoint conversation boundary mismatch")
    payload = thaw_json(task.state.get("operation"))
    if payload is None:
        return None
    if isinstance(payload, dict) and "task_policy" in task.state:
        payload["task_policy"] = thaw_json(task.state["task_policy"])
    # Older formats have no durable counters. Preserve their exact execution
    # position; migration never invents a received reply or restarts execution.
    if isinstance(payload, dict) and type(payload.get("schema_version")) is int and payload["schema_version"] in {1, 2}:
        from codey.runtime.core.operation_payload import RuntimeExecutionCounters

        safe_positions = {"accepted", "terminal", "writer_settled", "repair_settled",
                          "completion_proof_recorded", "repair_context_admitted"}
        if payload.get("leaf") not in safe_positions or payload.get("stop_reason") in {"approval", "question"}:
            raise RuntimeOperationTransitionError("legacy execution counters are unavailable; migration cannot reset budgets")
        payload = {**payload, "schema_version": 5, "counters": RuntimeExecutionCounters().to_payload()}
    if isinstance(payload, dict) and type(payload.get("schema_version")) is int and payload["schema_version"] in {3, 4}:
        # Schema 4 only adds a prepared-request position. Existing schema-3
        # positions and budgets keep their meaning; no execution is inferred.
        payload = {**payload, "schema_version": 5}
    state = RuntimeOperationState.from_payload(payload)
    if state is None or state.run_id != task.task_id or state.session_id != session_id:
        raise RuntimeOperationTransitionError("invalid durable operation checkpoint")
    return state


def _delivery_rows(snapshot: dict[str, Any]) -> list[dict[str, object]]:
    from codey.runtime.effects.tool_result_delivery import delivery_record_entry

    intent = snapshot["intent"]
    rows = [delivery_record_entry(intent)]
    base = {key: intent[key] for key in (
        "schema_version", "effect_kind", "batch_id", "session_id", "run_id", "lane", "operation_id", "created_at",
    )}
    for field, kind, ref in (
        ("send_attempts", "send_attempt", "delivery_attempt"),
        ("superseded_effect_ids", "send_superseded", "delivery_superseded"),
        ("delivered_effect_ids", "delivered", "delivery_delivered"),
    ):
        for identity in snapshot[field]:
            rows.append(delivery_record_entry({**base, "record_kind": kind,
                "ref": f"{ref}:{intent['batch_id']}:{identity}", "provider_effect_id": identity}))
    if snapshot["is_recovered"]:
        rows.append(delivery_record_entry({**base, "record_kind": "recovered",
            "ref": f"delivery_recovered:{intent['batch_id']}",
            "recovered_effect_ids": snapshot["recovered_effect_ids"],
            "recovered_reads": snapshot["recovered_reads"], "recovered_lookups": snapshot["recovered_lookups"]}))
    if snapshot["is_abandoned"]:
        rows.append(delivery_record_entry({**base, "record_kind": "abandoned",
            "ref": f"delivery_abandoned:{intent['batch_id']}", "reason": snapshot["abandoned_reason"]}))
    return rows


def checkpoint_entries(task: TaskCheckpoint, effects: tuple[EffectRecord, ...], *,
                       session_id: str) -> tuple[RuntimeLogEntry, ...]:
    from codey.runtime.core.operation_state import (
        LEAF_TERMINAL,
        RuntimeOperationTransitionError,
        operation_started_entry,
        operation_state_entry,
        outcome_for_terminal,
    )
    from codey.runtime.effects.effect_records import (
        RuntimeEffectIntent,
        RuntimeEffectSettlement,
        effect_intent_entry,
        effect_settlement_entry,
    )

    state = operation_from_checkpoint(task, session_id)
    if state is None:
        return ()
    rows = [operation_started_entry(state), operation_state_entry(state)]
    for effect in effects:
        # Shell approvals are owned by the durable approval state machine;
        # their receipt shape is intentionally outside the runtime projection.
        if effect.category == "shell":
            continue
        rows.append(effect_intent_entry(RuntimeEffectIntent.from_payload(cast(dict[str, Any], thaw_json(effect.intent)))))
        if effect.settlement is not None:
            rows.append(effect_settlement_entry(RuntimeEffectSettlement.from_payload(
                cast(dict[str, Any], thaw_json(effect.settlement)))))
    snapshots = thaw_json(task.state.get("delivery_batches", ()))
    if not isinstance(snapshots, list):
        raise RuntimeOperationTransitionError("invalid delivery checkpoint")
    for snapshot in snapshots:
        if not isinstance(snapshot, dict):
            raise RuntimeOperationTransitionError("invalid delivery batch snapshot")
        rows.extend(_delivery_rows(snapshot))
    if state.leaf == LEAF_TERMINAL:
        rows.append({"lane": state.lane, "operation_id": state.operation_id,
                     "kind": "operation_settled", "payload": outcome_for_terminal(state).to_payload()})
    return tuple(_entry(session_id, row) for row in rows)


def delivery_snapshot(batch: DeliveryBatchProjection) -> dict[str, object]:
    return {
        "intent": batch.intent.to_payload(), "send_attempts": list(batch.send_attempts),
        "superseded_effect_ids": list(batch.superseded_effect_ids),
        "delivered_effect_ids": list(batch.delivered_effect_ids),
        "is_recovered": batch.is_recovered, "recovered_effect_ids": list(batch.recovered_effect_ids),
        "recovered_reads": batch.recovered_reads, "recovered_lookups": batch.recovered_lookups,
        "is_abandoned": batch.is_abandoned, "abandoned_reason": batch.abandoned_reason,
    }



def _apply_boundary_transition(mutation: DurableMutation, state: RuntimeOperationState) -> DurableMutation:
    from codey.runtime.core.operation_state import RuntimeOperationTransitionError

    if mutation.task is None:
        raise RuntimeOperationTransitionError("boundary lost its task")
    payload = state.to_payload()
    payload.pop("task_policy", None)
    return replace(mutation, task=replace(mutation.task, state={**mutation.task.state, "operation": payload}))


def _effect_changes(projections: Any, effects: tuple[EffectRecord, ...]) -> tuple[list[EffectRecord], list[EffectRecord]]:
    from codey.runtime.core.operation_state import RuntimeOperationTransitionError

    known = {effect.effect_id: effect for effect in effects}
    intents: list[EffectRecord] = []
    settlements: list[EffectRecord] = []
    for effect in projections:
        intent = effect.intent
        record = EffectRecord(intent.effect_id, intent.run_id, intent.effect_category, str(intent.replay_class),
            intent.args_digest, effect.settlement.status if effect.settlement else "pending",
            intent.to_payload(), effect.settlement.to_payload() if effect.settlement else None)
        previous = known.get(record.effect_id)
        if previous is None:
            if record.settlement is not None:
                raise RuntimeOperationTransitionError("effect must commit intent before settlement")
            intents.append(record)
        elif previous != record:
            # Runtime projections may normalize the status label from the
            # settlement payload (for example ``interrupted``).  Once a
            # durable settlement exists, replaying the same receipt is an
            # idempotent projection step; compare the settlement fact,
            # not its derived status spelling.
            if (previous.settlement is not None
                    and record.settlement is not None
                    and previous.settlement == record.settlement):
                continue
            if previous.settlement is not None or record.settlement is None:
                raise RuntimeOperationTransitionError("effect outcome is immutable")
            settlements.append(record)
    return intents, settlements


def mutate_checkpoint(durable: DurableSession, session_id: str, run_id: str,
                      derive_rows: Callable[..., Any], *,
                      messages: tuple[tuple[str, Mapping[str, object]], ...] = (),
                      artifacts: tuple[ArtifactRecord, ...] = (),
                      generation_boundary: str | None = None,
                      boundary_result: list[object] | None = None,
                      boundary_transition: Callable[..., RuntimeOperationState] | None = None) -> None:
    from codey.runtime.core.operation_state import RuntimeOperationTransitionError
    from codey.runtime.log.session_projection import reduce_session
    from codey.runtime.log.session_view import load_session_view

    def derive(task: TaskCheckpoint, effects: tuple[EffectRecord, ...], revision: int) -> DurableMutation | None:
        entries = checkpoint_entries(task, effects, session_id=session_id)
        rows = tuple(derive_rows(reduce_session(entries), entries))
        if not rows:
            return None
        next_entries = entries + tuple(_entry(session_id, row) for row in rows)
        view = load_session_view(next_entries, session_id=session_id, run_id=run_id)
        if view.state is None:
            raise RuntimeOperationTransitionError("runtime mutation lost operation state")
        intents, settlements = _effect_changes(view.effects, effects)
        next_task = replace(task, revision=task.revision + 1)
        if intents:
            from codey.durable.errors import CommitConflict
            from codey.durable.model import TaskStatus

            if task.status != TaskStatus.RUNNING:
                raise CommitConflict("runtime effects require a running task")
            next_task = with_effect_intents(task, tuple(intents))
        for settlement in settlements:
            next_task = with_effect_settlement(next_task, settlement)
        operation_payload = view.state.to_payload()
        operation_payload.pop("task_policy", None)
        policy = {"task_policy": view.state.task_policy} if view.state.task_policy else {}
        next_task = replace(next_task, revision=task.revision + 1, state={
            **next_task.state, **policy, "operation": operation_payload,
            "delivery_batches": [delivery_snapshot(batch) for batch in view.batches],
        })
        head, _revision = durable.head(session_id)
        appended: list[EntryDraft] = []
        for kind, payload in messages:
            entry = EntryDraft(f"entry-{uuid.uuid4().hex}", session_id, head, kind, payload, by_task_id=run_id)
            appended.append(entry)
            head = entry.entry_id
        mutation = DurableMutation(
            session_id, revision, task=next_task, entries=tuple(appended),
            effects=tuple(intents), settlements=tuple(settlements), artifacts=_unregistered_artifacts(durable, artifacts),
        )
        if generation_boundary is not None:
            selected, mutation = durable.merge_generation_boundary(
                mutation, task_id=run_id, at=generation_boundary,
            )
            if boundary_transition is not None:
                mutation = _apply_boundary_transition(mutation, boundary_transition(view.state, selected))
            if boundary_result is not None:
                boundary_result[:] = selected
        return mutation

    # A user stop may advance the task checkpoint while the provider worker is
    # settling its runtime operation.  Rebuild the mutation from the newer
    # checkpoint rather than turning that expected cancellation race into an
    # unhandled worker failure.  The reducer is pure and the durable CAS still
    # rejects any competing write after the bounded retry budget.
    from codey.durable.errors import CommitConflict

    for attempt in range(3):
        try:
            durable.transact_task(run_id, derive)
            return
        except CommitConflict:
            if attempt == 2:
                raise


def _unregistered_artifacts(durable: Any, artifacts: tuple[ArtifactRecord, ...]) -> tuple[ArtifactRecord, ...]:
    """Reuse immutable content references inside the same transaction read line."""
    from codey.durable.errors import CommitConflict

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
