"""Private durable execution history, separate from public commit projections.

The commit envelope keeps the checkpoint and changed effects in the same
transaction as their current rows. Reads at a historical cursor never consult
today's checkpoint or execute an external effect.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import cast

from codey.durable.model import DurableMutation, EffectRecord, TaskCheckpoint, require_checkpoint_version, thaw_json
from codey.types.json_value import require_json_string
from codey.utils.object_fields import mapping_fields, sequence_items

WORKFLOW_TIMELINE_VERSION = 1


@dataclass(frozen=True)
class TaskTransition:
    commit_seq: int
    task: TaskCheckpoint
    effect_changes: tuple[EffectRecord, ...]
    head_entry_id: str | None


@dataclass(frozen=True)
class TaskSnapshot:
    commit_seq: int
    task: TaskCheckpoint
    effects: tuple[EffectRecord, ...]
    head_entry_id: str | None


@dataclass(frozen=True)
class TaskSettlementHistory:
    task: TaskCheckpoint
    settlements: tuple[tuple[int, EffectRecord], ...]


def effect_from_payload(effect: Mapping[str, object]) -> EffectRecord:
    intent, settlement = effect["intent"], effect["settlement"]
    if not mapping_fields(intent) or (settlement is not None and not mapping_fields(settlement)):
        raise ValueError("invalid timeline effect payload")
    return EffectRecord(
        effect_id=str(effect["effect_id"]), task_id=str(effect["task_id"]),
        category=str(effect["category"]), replay_class=str(effect["replay_class"]),
        args_digest=str(effect["args_digest"]), status=str(effect["status"]),
        intent=intent, settlement=settlement,
    )


def settlement_histories(
    task_ids: tuple[str, ...], rows: Iterable[tuple[int, object]],
) -> tuple[TaskSettlementHistory, ...]:
    """Project original settlements and only the latest checkpoint at the cursor.

    Historical checkpoint payloads remain available through task_history. A
    settlement read does not need to instantiate every revision of their
    unchanged frozen execution input.
    """
    wanted = set(task_ids)
    latest: dict[str, Mapping[str, object]] = {}
    settlements: dict[str, list[tuple[int, EffectRecord]]] = {task_id: [] for task_id in task_ids}
    for sequence, raw in rows:
        if not mapping_fields(raw):
            raise ValueError("invalid workflow timeline payload")
        if not raw:
            continue
        candidates = timeline_task_rows(raw)
        present: set[str] = set()
        for task in candidates:
            if not mapping_fields(task):
                continue
            identity = task.get("task_id")
            if isinstance(identity, str) and identity in wanted:
                latest[identity] = task
                present.add(identity)
        if not present:
            continue
        effects = raw["effects"]
        if not sequence_items(effects):
            raise ValueError("invalid timeline effects")
        for effect in effects:
            if not mapping_fields(effect):
                raise ValueError("invalid timeline effect")
            identity = effect.get("task_id")
            if isinstance(identity, str) and identity in present and effect.get("settlement") is not None:
                settlements[identity].append((sequence, effect_from_payload(effect)))
    return tuple(TaskSettlementHistory(checkpoint_from_payload(latest[task_id]), tuple(settlements[task_id]))
                 for task_id in task_ids if task_id in latest)


def checkpoint_payload(task: TaskCheckpoint) -> dict[str, object]:
    return {
        "task_id": task.task_id, "conversation_id": task.conversation_id,
        "submission_id": task.submission_id, "phase": task.phase, "revision": task.revision,
        "provider_snapshot": thaw_json(task.provider_snapshot), "state": thaw_json(task.state),
        "current_turn_id": task.current_turn_id, "pending_effect_id": task.pending_effect_id,
        "status": task.status, "schema_version": task.schema_version,
    }


def checkpoint_from_payload(raw: Mapping[str, object]) -> TaskCheckpoint:
    return TaskCheckpoint(
        task_id=str(raw["task_id"]), conversation_id=str(raw["conversation_id"]),
        submission_id=str(raw["submission_id"]), phase=require_json_string(raw["phase"], "task phase"),
        revision=cast(int, raw["revision"]),
        provider_snapshot=cast(Mapping[str, object], raw["provider_snapshot"]),
        state=cast(Mapping[str, object], raw["state"]),
        current_turn_id=cast(str | None, raw["current_turn_id"]),
        pending_effect_id=cast(str | None, raw["pending_effect_id"]),
        status=str(raw["status"]), schema_version=require_checkpoint_version(raw["schema_version"]),
    )


def execution_payload(mutation: DurableMutation, head: str | None) -> dict[str, object]:
    tasks = ((mutation.task,) if mutation.task is not None else ()) + mutation.task_updates
    if not tasks:
        return {}
    changes = ((mutation.effect,) if mutation.effect is not None else ()) + mutation.effects + mutation.settlements
    return {
        "schema_version": WORKFLOW_TIMELINE_VERSION,
        "tasks": [checkpoint_payload(task) for task in tasks],
        "head_entry_id": head,
        "effects": [
            {
                "effect_id": effect.effect_id, "task_id": effect.task_id,
                "category": effect.category, "replay_class": effect.replay_class,
                "args_digest": effect.args_digest, "status": effect.status,
                "intent": thaw_json(effect.intent), "settlement": thaw_json(effect.settlement),
            }
            for effect in changes
        ],
    }


def timeline_task_rows(raw: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    if (set(raw) != {"schema_version", "tasks", "head_entry_id", "effects"}
            or type(raw["schema_version"]) is not int or raw["schema_version"] != WORKFLOW_TIMELINE_VERSION):
        raise ValueError("unsupported workflow timeline schema")
    rows = raw["tasks"]
    if not sequence_items(rows) or not rows:
        raise ValueError("invalid workflow timeline tasks")
    tasks: list[Mapping[str, object]] = []
    for row in rows:
        if not mapping_fields(row):
            raise ValueError("invalid workflow timeline task")
        tasks.append(row)
    return tuple(tasks)


def transition_from_payload(sequence: int, raw: Mapping[str, object], task_id: str) -> TaskTransition:
    raw_task = next((task for task in timeline_task_rows(raw) if task.get("task_id") == task_id), None)
    if raw_task is None:
        raise KeyError(f"task {task_id!r} is not present in timeline commit")
    effects = cast(list[dict[str, object]], raw["effects"])
    return TaskTransition(
        sequence,
        checkpoint_from_payload(raw_task),
        tuple(effect_from_payload(effect) for effect in effects if effect.get("task_id") == task_id),
        cast(str | None, raw["head_entry_id"]),
    )


def snapshot_at(transitions: tuple[TaskTransition, ...]) -> TaskSnapshot:
    if not transitions:
        raise KeyError("task has no checkpoint at this commit")
    effects: dict[str, EffectRecord] = {}
    for transition in transitions:
        effects.update((effect.effect_id, effect) for effect in transition.effect_changes)
    latest = transitions[-1]
    return TaskSnapshot(latest.commit_seq, latest.task, tuple(effects.values()), latest.head_entry_id)


def public_commit_payload(payload: Mapping[str, object]) -> dict[str, object]:
    return {key: value for key, value in payload.items() if key != "_workflow"}
