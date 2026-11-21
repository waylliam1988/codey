"""Scope-aware canonical comparisons; free-text relations remain proposals."""

from __future__ import annotations

from codey.memory.contracts import MemoryConflict, MemoryItem


def relation(new: MemoryItem, old: MemoryItem, *, suggested: bool = False) -> MemoryConflict | None:
    if new.scope != old.scope or new.kind != old.kind or new.item_id == old.item_id:
        return None
    refs = (*old.source_refs, *new.source_refs)
    same_slot = bool(new.slot and new.slot == old.slot)
    if same_slot and new.context != old.context:
        return MemoryConflict(old.item_id, "context_exception", refs)
    if same_slot and new.value and old.value:
        return MemoryConflict(old.item_id, "equivalent" if new.value == old.value else "conflicting", refs)
    if new.text.casefold().strip() == old.text.casefold().strip() and new.context == old.context:
        return MemoryConflict(old.item_id, "equivalent", refs)
    return MemoryConflict(old.item_id, "suspected_conflict", refs) if suggested or same_slot else None
