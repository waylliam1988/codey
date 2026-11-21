"""The single slot registry for validation, conflict keys and family routing."""

from __future__ import annotations

import re
from dataclasses import dataclass

from codey.memory.contracts import MemoryItem


@dataclass(frozen=True)
class MemorySlot:
    family: str
    values: tuple[str, ...] = ()


SLOTS: dict[str, MemorySlot] = {
    "response.language": MemorySlot("response_language"),
    "response.detail": MemorySlot("response_detail", ("brief", "detailed")),
    "code.language": MemorySlot("code_examples", ("python", "rust", "javascript", "typescript", "go", "java", "c", "cpp", "csharp")),
    "verification.command": MemorySlot("verification_habit"),
    "repair.pattern": MemorySlot("repair"),
}
FAMILIES = ("manual", "continuity", *(row.family for row in SLOTS.values()))


def family_for(item: MemoryItem) -> str:
    if item.kind == "experience":
        return "continuity"
    slot = SLOTS.get(item.slot)
    return slot.family if slot is not None else "manual"


def validate_slot(item: MemoryItem) -> None:
    slot = SLOTS.get(item.slot)
    if slot is None:
        return
    if not item.value or item.value != item.value.strip():
        raise ValueError("a registered memory slot requires a normalized value")
    if slot.values and item.value not in slot.values:
        raise ValueError("value is not supported by this memory slot")
    if item.slot == "response.language" and re.fullmatch(r"[a-z]{2,3}(?:-[A-Z]{2})?", item.value) is None:
        raise ValueError("language value must be a language code")
