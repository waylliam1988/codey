"""Encode durable state for tests of the retired runtime log format."""
from __future__ import annotations

from codey.durable.model import EffectRecord, TaskCheckpoint, thaw_json
from codey.runtime.log.entries import RuntimeLogEntry
from codey.runtime.write.checkpoint import operation_from_checkpoint
from codey.types.json_value import canonical_json_object, require_json_string, require_json_strings


def _entry(session_id: str, row: dict[str, object]) -> RuntimeLogEntry:
    from codey.runtime.log.entries import RuntimeLogEntry

    return RuntimeLogEntry(session_id=session_id, lane=require_json_string(row["lane"], "lane"),
        operation_id=require_json_string(row["operation_id"], "operation_id"),
        kind=require_json_string(row["kind"], "kind"), payload=canonical_json_object(row["payload"]))


def _delivery_rows(snapshot: dict[str, object]) -> list[dict[str, object]]:
    from codey.runtime.effects.tool_result_delivery import delivery_record_entry

    intent = canonical_json_object(snapshot["intent"])
    rows = [delivery_record_entry(intent)]
    base = {key: intent[key] for key in (
        "schema_version", "effect_kind", "batch_id", "session_id", "run_id", "lane", "operation_id", "created_at",
    )}
    for field, kind, ref in (
        ("send_attempts", "send_attempt", "delivery_attempt"),
        ("superseded_effect_ids", "send_superseded", "delivery_superseded"),
        ("delivered_effect_ids", "delivered", "delivery_delivered"),
    ):
        for identity in require_json_strings(snapshot[field], field):
            row: dict[str, object] = {**base, "record_kind": kind,
                "ref": f"{ref}:{intent['batch_id']}:{identity}", "provider_effect_id": identity}
            rows.append(delivery_record_entry(row))
    if snapshot["is_recovered"]:
        recovered: dict[str, object] = {**base, "record_kind": "recovered",
            "ref": f"delivery_recovered:{intent['batch_id']}",
            "recovered_effect_ids": snapshot["recovered_effect_ids"],
            "recovered_reads": snapshot["recovered_reads"], "recovered_lookups": snapshot["recovered_lookups"]}
        rows.append(delivery_record_entry(recovered))
    if snapshot["is_abandoned"]:
        abandoned: dict[str, object] = {**base, "record_kind": "abandoned",
            "ref": f"delivery_abandoned:{intent['batch_id']}", "reason": snapshot["abandoned_reason"]}
        rows.append(delivery_record_entry(abandoned))
    return rows


def checkpoint_entries(task: TaskCheckpoint, effects: tuple[EffectRecord, ...], *,
                       session_id: str) -> tuple[RuntimeLogEntry, ...]:
    from codey.runtime.core.operation_payload import LEAF_TERMINAL, RuntimeOperationTransitionError
    from codey.runtime.core.operation_state import operation_started_entry, operation_state_entry, outcome_for_terminal
    from codey.runtime.effects.effect_records import (
        RuntimeEffectIntent,
        RuntimeEffectSettlement,
        effect_intent_entry,
        effect_settlement_entry,
    )

    state = operation_from_checkpoint(task, session_id)
    if state is None:
        return ()
    rows: list[dict[str, object]] = [operation_started_entry(state), operation_state_entry(state)]
    for effect in effects:
        # Shell approvals are owned by the durable approval state machine;
        # their receipt shape is intentionally outside the runtime projection.
        if effect.category == "shell":
            continue
        intent = canonical_json_object(effect.intent)
        rows.append(effect_intent_entry(RuntimeEffectIntent.from_payload(intent)))
        if effect.settlement is not None:
            settlement = canonical_json_object(effect.settlement)
            rows.append(effect_settlement_entry(RuntimeEffectSettlement.from_payload(settlement)))
    snapshots = thaw_json(task.state.get("delivery_batches", ()))
    if not isinstance(snapshots, list):
        raise RuntimeOperationTransitionError("invalid delivery checkpoint")
    snapshot_values: list[object] = snapshots
    for snapshot in snapshot_values:
        if not isinstance(snapshot, dict):
            raise RuntimeOperationTransitionError("invalid delivery batch snapshot")
        rows.extend(_delivery_rows(dict(canonical_json_object(snapshot))))
    if state.leaf == LEAF_TERMINAL:
        outcome: dict[str, object] = outcome_for_terminal(state).to_payload()
        rows.append({"lane": state.lane, "operation_id": state.operation_id,
                     "kind": "operation_settled", "payload": outcome})
    return tuple(_entry(session_id, row) for row in rows)
