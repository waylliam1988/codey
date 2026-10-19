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


def mutate_checkpoint(durable: DurableSession, session_id: str, run_id: str,
                      derive_rows: Callable[..., Any], *,
                      messages: tuple[tuple[str, Mapping[str, object]], ...] = (),
                      artifacts: tuple[ArtifactRecord, ...] = ()) -> None:
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
        known = {effect.effect_id: effect for effect in effects}
        intents: list[EffectRecord] = []
        settlements: list[EffectRecord] = []
        for effect in view.effects:
            intent = effect.intent
            record = EffectRecord(intent.effect_id, run_id, intent.effect_category, str(intent.replay_class),
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
                if (task.state.get("cancel_requested") and previous.settlement is not None
                        and record.settlement is not None):
                    # Cancellation has already sealed the canonical effect
                    # outcome.  Runtime JSONL may still describe the pre-stop
                    # tool denial; it is a projection and cannot overwrite
                    # the durable interrupted settlement.
                    continue
                if (previous.settlement is not None
                        and record.settlement is not None
                        and previous.settlement == record.settlement):
                    continue
                if previous.settlement is not None or record.settlement is None:
                    raise RuntimeOperationTransitionError("effect outcome is immutable")
                settlements.append(record)
        next_task = replace(task, revision=task.revision + 1)
        if intents:
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
        return DurableMutation(session_id, revision, task=next_task, entries=tuple(appended),
                               effects=tuple(intents), settlements=tuple(settlements), artifacts=artifacts)

    durable.transact_task(run_id, derive)
