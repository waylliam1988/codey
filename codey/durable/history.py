"""History metadata and readers bound to one storage snapshot.

Ancestry and ordinals may traverse metadata for the visible path. Only selected
page entries, task checkpoints and response artifacts load their full bodies.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from codey.durable.ancestry import _local_path, path_from_local
from codey.durable.history_index import DisplayNode, DisplayVersion
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
    def node(self, identity: str) -> DisplayNode: ...

    def version(self, entry_id: str | None) -> DisplayVersion: ...

    def display_head(self, conversation_id: str) -> tuple[str | None, int, int]: ...

    def cursor_key(self) -> bytes: ...

    def navigation_items(self, identities: list[str]) -> list[dict[str, object]]: ...

    def display_entries(self, identities: list[str]) -> tuple[HistoryEntry, ...]: ...

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
    """Immutable-record snapshot copied during short memory-store admission."""

    def __init__(self, store: MemoryDurableStore) -> None:
        # Copy references to immutable records while holding the store lock.
        # The request then owns its snapshot and ordinary writes may continue.
        self._heads = dict(store._heads)
        self._parents = dict(store._parents)
        self._generations = dict(store._path_generations)
        self._versions = dict(store._display_versions)
        self._nodes = dict(store._display_nodes)
        self._entries = dict(store._entry_by_id)
        self._tasks = dict(store._tasks)
        self._artifacts = dict(store._artifacts)
        self._key = store._history_cursor_key

    def node(self, identity: str) -> DisplayNode:
        return self._nodes[identity]

    def version(self, entry_id: str | None) -> DisplayVersion:
        return DisplayVersion() if entry_id is None else self._versions[entry_id]

    def display_head(self, conversation_id: str) -> tuple[str | None, int, int]:
        head, revision = self.head(conversation_id)
        return head or self._parents[conversation_id][1], revision, self._generations[conversation_id]

    def cursor_key(self) -> bytes:
        return self._key

    def navigation_items(self, identities: list[str]) -> list[dict[str, object]]:
        return [{"id": identity, "text": str(self._entries[identity].payload.get("text") or "")[:240]}
                for identity in identities]

    def display_entries(self, identities: list[str]) -> tuple[HistoryEntry, ...]:
        return tuple(HistoryEntry(entry.entry_id, entry.conversation_id, entry.parent_id, entry.kind,
                                  entry.by_task_id, {}) for identity in identities for entry in (self._entries[identity],))

    def head(self, conversation_id: str) -> tuple[str | None, int]:
        return self._heads[conversation_id]

    def conversation_parent(self, conversation_id: str) -> tuple[str | None, str | None]:
        return self._parents[conversation_id]

    def local_path(self, conversation_id: str, leaf_id: str) -> tuple[HistoryEntry, ...]:
        local = {entry.entry_id: entry for entry in self._entries.values() if entry.conversation_id == conversation_id}
        path = _local_path(local, leaf_id) if leaf_id in local else ()
        return tuple(HistoryEntry(entry.entry_id, entry.conversation_id, entry.parent_id, entry.kind,
                                  entry.by_task_id, {
                                      key: entry.payload.get(key)
                                      for key in ("head_marker", "edited_entry_id", "edit_parent_entry_id")
                                  }) for entry in path)

    def run_ids(self, conversation_id: str) -> set[str]:
        return {entry.by_task_id for entry in self._entries.values()
                if entry.conversation_id == conversation_id and entry.by_task_id}

    def select_entries(self, selected: tuple[HistoryEntry, ...]) -> tuple[EntryDraft, ...]:
        return tuple(self._entries[entry.entry_id] for entry in selected)

    def task(self, task_id: str) -> TaskCheckpoint:
        return self._tasks[task_id]

    def artifact(self, handle: str) -> ArtifactRecord:
        return self._artifacts[handle]
