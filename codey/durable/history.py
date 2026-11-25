"""History metadata and readers bound to one storage snapshot.

Ancestry and ordinals may traverse metadata for the visible path. Only selected
page entries, task checkpoints and response artifacts load their full bodies.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from codey.durable.ancestry import path_from_local
from codey.durable.history_index import DisplayNode, DisplayVersion
from codey.durable.model import ArtifactRecord, EntryDraft, TaskCheckpoint


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
