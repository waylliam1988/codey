"""History metadata and readers bound to one storage snapshot.

Ancestry and ordinals may traverse metadata for the visible path. Only selected
page entries, task checkpoints and response artifacts load their full bodies.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from codey.durable.ancestry import _local_path, path_from_local
from codey.durable.model import ArtifactRecord, EntryDraft, TaskCheckpoint

if TYPE_CHECKING:
    from codey.durable.memory import MemoryDurableStore


@dataclass(frozen=True)
class HistoryEntry:
    entry_id: str
    conversation_id: str
    parent_id: str | None
    kind: str
    by_task_id: str | None
    payload: Mapping[str, object]


class HistoryReader(Protocol):
    def head(self, conversation_id: str) -> tuple[str | None, int]: ...

    def conversation_parent(self, conversation_id: str) -> tuple[str | None, str | None]: ...

    def local_path(self, conversation_id: str, leaf_id: str) -> tuple[HistoryEntry, ...]: ...

    def run_ids(self, conversation_id: str) -> set[str]: ...

    def select_entries(self, selected: tuple[HistoryEntry, ...]) -> tuple[EntryDraft, ...]: ...

    def task(self, task_id: str) -> TaskCheckpoint: ...

    def artifact(self, handle: str) -> ArtifactRecord: ...


def history_path(reader: HistoryReader, conversation_id: str, head: str | None) -> tuple[HistoryEntry, ...]:
    path = path_from_local(conversation_id, head, local_path=reader.local_path, parent=reader.conversation_parent)
    for index in range(len(path) - 1, -1, -1):
        if path[index].payload.get("head_marker") is True:
            path = path[index:]
            break
    return tuple(entry for entry in path if entry.kind not in {"context_snapshot", "control_config", "control_receipt"})


class MemoryHistoryReader:
    """Used while the memory store holds its lock for the complete page."""

    def __init__(self, store: MemoryDurableStore) -> None:
        self._store = store

    def head(self, conversation_id: str) -> tuple[str | None, int]:
        return self._store.head(conversation_id)

    def conversation_parent(self, conversation_id: str) -> tuple[str | None, str | None]:
        return self._store.conversation_parent(conversation_id)

    def local_path(self, conversation_id: str, leaf_id: str) -> tuple[HistoryEntry, ...]:
        local = {entry.entry_id: entry for entry in self._store.entries(conversation_id)}
        path = _local_path(local, leaf_id) if leaf_id in local else ()
        return tuple(HistoryEntry(entry.entry_id, entry.conversation_id, entry.parent_id, entry.kind,
                                  entry.by_task_id, {
                                      key: entry.payload.get(key)
                                      for key in ("head_marker", "edited_entry_id", "edit_parent_entry_id")
                                  }) for entry in path)

    def run_ids(self, conversation_id: str) -> set[str]:
        return {entry.by_task_id for entry in self._store.entries(conversation_id) if entry.by_task_id}

    def select_entries(self, selected: tuple[HistoryEntry, ...]) -> tuple[EntryDraft, ...]:
        scopes = {entry.conversation_id for entry in selected}
        entries = {entry.entry_id: entry for scope in scopes for entry in self._store.entries(scope)}
        return tuple(entries[entry.entry_id] for entry in selected)

    def task(self, task_id: str) -> TaskCheckpoint:
        return self._store.task(task_id)

    def artifact(self, handle: str) -> ArtifactRecord:
        return self._store.artifact(handle)
