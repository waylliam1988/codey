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
_TEMPORARY_SCOPE = re.compile(r"\b(?:for|in) this (?:reply|answer|example)(?: only)?\b|(?:这次|本次|本轮)", re.I)
_PROJECT_SCOPE = re.compile(r"\bin this project\b|本项目", re.I)
_EXAMPLE_LANGUAGE = re.compile(
    r"^\s*(?:please\s+)?(?:use ([a-z]+)\s+for\s+(?:(?:the|this)\s+)?(?:code\s+)?examples?"
    r"|show [^.!?;,\n]{0,80}\bexample in ([a-z]+))\b"
    r"|^\s*(?:请)?(?:代码)?示例(?:请)?(?:使用|用)\s*([a-z]+)\b"
    r"|^\s*(?:请)?(?:使用|用)\s*([a-z]+)\s*(?:写|展示|给出)?(?:代码)?示例",
    re.I,
)


def _example_language(text: str, scope: re.Pattern[str]) -> MemorySlotOverride | None:
    clause = re.split(r"[.!?;。！？；\n]", instruction_text(text), maxsplit=1)[0]
    if scope.search(clause) is None:
        return None
    directive = scope.sub("", clause).strip(" ,，的")
    matched = _EXAMPLE_LANGUAGE.match(directive)
    if matched is None:
        return None
    groups: tuple[object, ...] = matched.groups()
    value = next(group.casefold() for group in groups if isinstance(group, str))
    return MemorySlotOverride(_CODE_LANGUAGE, value) if value in SLOTS[_CODE_LANGUAGE].values else None


def temporary_slot_override(text: str) -> MemorySlotOverride | None:
    return _example_language(text, _TEMPORARY_SCOPE)


def contextual_slot_correction(text: str) -> MemorySlotOverride | None:
    return _example_language(text, _PROJECT_SCOPE)


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
