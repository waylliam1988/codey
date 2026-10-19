"""Resolve an entry path through immutable conversation fork caps."""

from __future__ import annotations

from collections.abc import Callable

from codey.durable.model import EntryDraft


def context_path(
    conversation_id: str,
    leaf_id: str | None,
    *,
    entries: Callable[[str], tuple[EntryDraft, ...]],
    parent: Callable[[str], tuple[str | None, str | None]],
) -> tuple[EntryDraft, ...]:
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

    path: tuple[EntryDraft, ...] = ()
    for scope, leaf in reversed(ancestry):
        if leaf is None:
            continue
        local = {entry.entry_id: entry for entry in entries(scope)}
        if leaf in local:
            path += _local_path(local, leaf)
            continue
        position = next((index for index, entry in enumerate(path) if entry.entry_id == leaf), None)
        if position is None:
            raise KeyError(f"entry {leaf} is not visible from conversation {scope}")
        path = path[:position + 1]
    return path


def _local_path(entries: dict[str, EntryDraft], leaf_id: str) -> tuple[EntryDraft, ...]:
    path: list[EntryDraft] = []
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
