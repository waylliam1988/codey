"""Private durable execution history, separate from public commit projections.

The commit envelope keeps the checkpoint and changed effects in the same
transaction as their current rows. Reads at a historical cursor never consult
today's checkpoint or execute an external effect.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from codey.durable.model import DurableMutation, EffectRecord, TaskCheckpoint, thaw_json


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


def checkpoint_payload(task: TaskCheckpoint) -> dict[str, object]:
    return {
        "task_id": task.task_id, "conversation_id": task.conversation_id,
        "submission_id": task.submission_id, "phase": task.phase, "revision": task.revision,
        "provider_snapshot": thaw_json(task.provider_snapshot), "state": thaw_json(task.state),
        "current_turn_id": task.current_turn_id, "pending_effect_id": task.pending_effect_id,
        "status": task.status, "schema_version": task.schema_version,
    }


def checkpoint_from_payload(raw: Mapping[str, object]) -> TaskCheckpoint:
    from codey.durable.model import migrate_checkpoint_phase

    phase, version = migrate_checkpoint_phase(str(raw["phase"]), cast(int, raw["schema_version"]))
    return TaskCheckpoint(
        task_id=str(raw["task_id"]), conversation_id=str(raw["conversation_id"]),
        submission_id=str(raw["submission_id"]), phase=phase,
        revision=cast(int, raw["revision"]),
        provider_snapshot=cast(Mapping[str, object], raw["provider_snapshot"]),
        state=cast(Mapping[str, object], raw["state"]),
        current_turn_id=cast(str | None, raw["current_turn_id"]),
        pending_effect_id=cast(str | None, raw["pending_effect_id"]),
        status=str(raw["status"]), schema_version=version,
    )


def execution_payload(mutation: DurableMutation, head: str | None) -> dict[str, object]:
    tasks = ((mutation.task,) if mutation.task is not None else ()) + mutation.task_updates
    if not tasks:
        return {}
    changes = ((mutation.effect,) if mutation.effect is not None else ()) + mutation.effects + mutation.settlements
    return {
        "schema_version": 1,
        # Keep the singular field for the original timeline format while
        # carrying every checkpoint changed by one atomic mutation.  Readers
        # can select the checkpoint belonging to the task they are replaying.
        "task": checkpoint_payload(tasks[0]),
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


def transition_from_payload(
    sequence: int,
    raw: Mapping[str, object],
    task_id: str | None = None,
) -> TaskTransition:
    if raw.get("schema_version") != 1:
        raise ValueError("unsupported workflow timeline schema")
    effects = cast(list[dict[str, object]], raw["effects"])
    raw_task: Mapping[str, object]
    task_rows = raw.get("tasks")
    if task_id is not None and isinstance(task_rows, (list, tuple)):
        selected = next(
            (item for item in task_rows if isinstance(item, Mapping) and item.get("task_id") == task_id),
            None,
        )
        if selected is None:
            raise KeyError(f"task {task_id!r} is not present in timeline commit")
        raw_task = selected
    else:
        raw_task = cast(Mapping[str, object], raw["task"])
    return TaskTransition(
        sequence,
        checkpoint_from_payload(raw_task),
        tuple(EffectRecord(
            effect_id=str(effect["effect_id"]), task_id=str(effect["task_id"]),
            category=str(effect["category"]), replay_class=str(effect["replay_class"]),
            args_digest=str(effect["args_digest"]), status=str(effect["status"]),
            intent=cast(Mapping[str, object], effect["intent"]),
            settlement=cast(Mapping[str, object] | None, effect["settlement"]),
        ) for effect in effects if task_id is None or effect.get("task_id") == task_id),
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
