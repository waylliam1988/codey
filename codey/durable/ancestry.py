"""Resolve an entry path through immutable conversation fork caps."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Protocol, TypeVar

from codey.durable.model import EntryDraft


class PathEntry(Protocol):
    @property
    def entry_id(self) -> str: ...

    @property
    def parent_id(self) -> str | None: ...

    @property
    def payload(self) -> Mapping[str, object]: ...


_Entry = TypeVar("_Entry", bound=PathEntry)


def context_path(
    conversation_id: str,
    leaf_id: str | None,
    *,
    entries: Callable[[str], tuple[EntryDraft, ...]],
    parent: Callable[[str], tuple[str | None, str | None]],
) -> tuple[EntryDraft, ...]:
    def local_path(scope: str, leaf: str) -> tuple[EntryDraft, ...]:
        local = {entry.entry_id: entry for entry in entries(scope)}
        return _local_path(local, leaf) if leaf in local else ()

    return path_from_local(conversation_id, leaf_id, local_path=local_path, parent=parent)


def path_from_local(
    conversation_id: str,
    leaf_id: str | None,
    *,
    local_path: Callable[[str, str], tuple[_Entry, ...]],
    parent: Callable[[str], tuple[str | None, str | None]],
) -> tuple[_Entry, ...]:
    """Resolve each ancestor before applying its child's cap.

    A cap can itself be inherited. In that case only the prefix ending at
    that entry is visible; intervening conversations contribute no tail.
    Callers provide reads bound to their transaction or mutation line.
    """
    ancestry: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    current: str | None = conversation_id
    cap = leaf_id
    while current is not None:
        if current in seen:
            raise ValueError("conversation ancestry cycle")
        seen.add(current)
        ancestry.append((current, cap))
        current, cap = parent(current)
        if current is None and cap is not None:
            raise ValueError("conversation cap has no parent")

    path: tuple[_Entry, ...] = ()
    for scope, leaf in reversed(ancestry):
        if leaf is None:
            continue
        tail = local_path(scope, leaf)
        if tail:
            if tail[0].payload.get("edited_entry_id"):
                # A replacement can rewind into an inherited fork prefix.
                bridge = tail[0].payload.get("edit_parent_entry_id")
                if bridge is None:
                    path = ()
                else:
                    position = next((i for i, entry in enumerate(path) if entry.entry_id == bridge), None)
                    if position is None:
                        raise KeyError(f"edit parent {bridge} is not visible")
                    if position is not None:
                        path = path[:position + 1]
            path += tail
            continue
        position = next((index for index, entry in enumerate(path) if entry.entry_id == leaf), None)
        if position is None:
            raise KeyError(f"entry {leaf} is not visible from conversation {scope}")
        path = path[:position + 1]
    return path


def _local_path(entries: dict[str, _Entry], leaf_id: str) -> tuple[_Entry, ...]:
    path: list[_Entry] = []
    seen: set[str] = set()
    current: str | None = leaf_id
    while current is not None:
        if current in seen:
            raise ValueError("conversation entry parent cycle")
        seen.add(current)
        entry = entries[current]
        path.append(entry)
        current = entry.parent_id
    return tuple(reversed(path))
