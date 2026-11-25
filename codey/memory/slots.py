"""The single slot registry for validation, conflict keys and family routing."""

from __future__ import annotations

import re
from dataclasses import dataclass

from codey.memory.contracts import MemoryItem, MemorySlotOverride
from codey.task.instruction_text import instruction_text


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

_CODE_LANGUAGE = "code.language"
_TEMPORARY_EXAMPLE = re.compile(
    r"^\s*(?:for this (?:reply|answer|example)(?: only)?,\s*"
    r"(?:use ([a-z]+)(?=\s+for (?:the |this )?(?:code )?example\b|[.!?;,\n]|$)"
    r"|show [^.!?;,\n]{0,80}\bexample in ([a-z]+)(?=[.!?;,\n]|$))"
    r"|(?:这次|本次|本轮)(?:的)?(?:代码)?示例(?:请)?用\s*([a-z]+)(?=[。！？；，,\n]|$))",
    re.I,
)
_CONTEXT_EXAMPLE = re.compile(r"^\s*(?:in this project,\s*use ([a-z]+) for code examples"
                              r"|本项目(?:的)?代码示例(?:请)?用\s*([a-z]+))[.!。！]?\s*$", re.I)


def _example_language(text: str, pattern: re.Pattern[str]) -> MemorySlotOverride | None:
    matched = pattern.match(instruction_text(text))
    if matched is None:
        return None
    groups: tuple[object, ...] = matched.groups()
    value = next(group.casefold() for group in groups if isinstance(group, str))
    return MemorySlotOverride(_CODE_LANGUAGE, value) if value in SLOTS[_CODE_LANGUAGE].values else None


def temporary_slot_override(text: str) -> MemorySlotOverride | None:
    return _example_language(text, _TEMPORARY_EXAMPLE)


def contextual_slot_correction(text: str) -> MemorySlotOverride | None:
    return _example_language(text, _CONTEXT_EXAMPLE)


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
