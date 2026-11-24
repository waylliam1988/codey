"""Maintain effect indexes without deciding operation execution phases."""
from __future__ import annotations

from dataclasses import replace

from codey.durable.errors import CommitConflict
from codey.durable.model import EffectRecord, TaskCheckpoint, TaskStatus, cancellation_receipts_only, thaw_json
from codey.types.json_value import require_json_strings
from codey.utils.object_fields import object_fields


def _effect_index(current: TaskCheckpoint) -> tuple[dict[str, object], list[str]]:
    state = thaw_json(current.state)
    if not object_fields(state):
        raise TypeError("task checkpoint state must be a JSON object")
    raw_pending = state.get("pending_effect_ids")
    pending = [] if raw_pending is None else require_json_strings(raw_pending, "pending effect IDs")
    return state, pending


def with_effect_intents(current: TaskCheckpoint, effects: tuple[EffectRecord, ...]) -> TaskCheckpoint:
    if not effects or any(effect.task_id != current.task_id or effect.status != "pending"
                          or effect.settlement is not None for effect in effects):
        raise ValueError("effect intents must be pending and belong to the task")
    if current.phase != "running" or current.status not in {TaskStatus.RUNNING, TaskStatus.WAITING} or (current.state.get("cancel_requested")
            and not cancellation_receipts_only(current, effects)):
        raise CommitConflict("task does not admit new external effects")
    state, pending = _effect_index(current)
    for effect in effects:
        if effect.effect_id not in pending:
            pending.append(effect.effect_id)
    return replace(current, revision=current.revision + 1,
                   state={**state, "pending_effect_ids": pending}, pending_effect_id=pending[-1])


def with_effect_settlement(current: TaskCheckpoint, effect: EffectRecord) -> TaskCheckpoint:
    if effect.task_id != current.task_id or effect.settlement is None or effect.status == "pending":
        raise ValueError("effect settlement requires a terminal outcome belonging to the task")
    state, pending = _effect_index(current)
    remaining = [item for item in pending if item != effect.effect_id]
    return replace(current, revision=current.revision + 1,
                   state={**state, "pending_effect_ids": remaining}, pending_effect_id=remaining[-1] if remaining else None)
