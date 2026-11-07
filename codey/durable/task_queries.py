"""Read-only task, effect, artifact, and approval facade."""

from __future__ import annotations

from codey.durable.model import (
    ApprovalRecord,
    ArtifactRecord,
    EffectRecord,
    TaskCheckpoint,
)
from codey.durable.query_line import QueryLine
from codey.durable.store import DurableStore
from codey.durable.timeline import TaskSnapshot, TaskTransition, snapshot_at


def read_task_snapshot(store: DurableStore, task_id: str, commit_seq: int) -> TaskSnapshot:
    """Read a historical checkpoint; the caller holds the kernel line."""
    if type(commit_seq) is not int or commit_seq < 0:
        raise ValueError("historical commit sequence must be a non-negative integer")
    return snapshot_at(store.task_history(task_id, through=commit_seq))


class TaskQueryFacade:
    """Own task and effect reads while sharing the session mutation line."""

    def __init__(self, store: DurableStore, query_line: QueryLine) -> None:
        self._store = store
        self._query_line = query_line

    def tasks(self) -> tuple[TaskCheckpoint, ...]:
        return self._query_line(lambda: tuple(self._store.tasks()))

    def pending_tasks(self) -> tuple[TaskCheckpoint, ...]:
        return self._query_line(lambda: tuple(self._store.pending_tasks()))

    def task(self, task_id: str) -> TaskCheckpoint:
        def read() -> TaskCheckpoint:
            return self._store.task(task_id)

        return self._query_line(read)

    def task_timeline(self, task_id: str) -> tuple[TaskTransition, ...]:
        return self._query_line(lambda: self._store.task_history(task_id))

    def task_at(self, task_id: str, commit_seq: int) -> TaskSnapshot:
        def read() -> TaskSnapshot:
            return read_task_snapshot(self._store, task_id, commit_seq)

        return self._query_line(read)

    def effects(self, task_id: str) -> tuple[EffectRecord, ...]:
        return self._query_line(lambda: tuple(self._store.effects_for_task(task_id)))

    def artifact(self, handle: str) -> ArtifactRecord:
        def read() -> ArtifactRecord:
            return self._store.artifact(handle)

        return self._query_line(read)

    def artifacts_for_task(self, task_id: str) -> tuple[ArtifactRecord, ...]:
        return self._query_line(lambda: self._store.artifacts_for_task(task_id))

    def artifacts_for_effect(self, effect_id: str) -> tuple[ArtifactRecord, ...]:
        return self._query_line(lambda: self._store.artifacts_for_effect(effect_id))

    def task_facts(self, task_id: str) -> tuple[TaskCheckpoint, tuple[EffectRecord, ...]]:
        def read() -> tuple[TaskCheckpoint, tuple[EffectRecord, ...]]:
            return self._store.task(task_id), tuple(self._store.effects_for_task(task_id))

        return self._query_line(read)

    def effect(self, effect_id: str) -> EffectRecord:
        def read() -> EffectRecord:
            return self._store.effect(effect_id)

        return self._query_line(read)

    def approval(self, approval_id: str) -> ApprovalRecord:
        def read() -> ApprovalRecord:
            return self._store.approval(approval_id)

        return self._query_line(read)


__all__ = ["QueryLine", "TaskQueryFacade"]
