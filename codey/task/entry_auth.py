"""User-entry authorization shared by HTTP and CLI/headless."""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from codey.task.model import derive_project_changes_required
from codey.task.web_intent import explicit_web_intent


@dataclass(frozen=True)
class EntryAuth:
    """User-authorized capabilities derived once at the submission boundary."""

    requested_capabilities: tuple[str, ...] = ()
    strict_research: bool = False
    project_changes_required: bool = False
    sources_open_required: bool = False
    denied_capabilities: tuple[str, ...] = ()


_READONLY_MUST_NOT_CHANGE_MARKERS = (
    "不要修改", "不要改", "不修改", "不改动", "只检查", "只读", "不要改动",
    "read-only", "readonly", "do not modify", "don't modify", "do not change", "don't change",
    "do not edit", "don't edit", "must not modify", "must not change", "must not edit",
)

_KNOWN_ENTRY_GRANTS = frozenset({
    "project.read", "project.write", "project.verify", "shell.approval",
    "web.read", "knowledge.read", "knowledge.write", "knowledge.link", "control",
})


def _explicit_readonly_task(task: str) -> bool:
    lowered = (task or "").lower()
    for marker in _READONLY_MUST_NOT_CHANGE_MARKERS:
        for match in re.finditer(re.escape(marker.lower()), lowered):
            suffix = lowered[match.end():].lstrip()
            # A request to preserve tests or one named file is a local task
            # constraint, not a denial of every implementation edit.
            scoped = re.match(
                r"(?:(?:the|existing|original)\s+)*(?:tests\b|test suite\b|[\w/-]+\.(?:py|js|ts|tsx|jsx|json|md|txt)\b)",
                suffix,
            )
            if marker.endswith((" modify", " change", " edit")) and scoped:
                remainder = re.split(r"[.!?;\n]", suffix[scoped.end():], maxsplit=1)[0]
                if not re.search(r"\b(?:any|all|other)\b.*\bfiles?\b", remainder):
                    continue
            return True
    return False


def derive_entry_auth(body: Mapping[str, object] | None, *, project: str | None = None) -> EntryAuth:
    """Derive entry auth from user submission only; never from model output.

    权限回答“可以做什么”，完成要求回答“必须做什么”：
    - 宽泛“查一下”不再视为联网授权，仅明确动作表达才授权；
    - 否定仅作用于同一分句的对应动作，不影响另一分句；
    - allow_web/requested 仅给“可以”，不自动等于“必须打开来源”；
    - 明确只读目标不生成必须修改要求。
    """
    data = dict(body) if body is not None else dict[str, object]()
    intent = str(data.get("intent") or "auto").strip().lower()
    task = str(data.get("task") or "")
    raw_requested = data.get("requested_capabilities", ())
    if isinstance(raw_requested, str):
        raw_requested = (raw_requested,)
    items: Iterable[object] = raw_requested if isinstance(raw_requested, Iterable) else ()
    requested: set[str] = set()
    for item in items:
        text = str(item or "").strip().lower()
        if text in _KNOWN_ENTRY_GRANTS:
            requested.add(text)
    if data.get("allow_web") is True:
        requested.add("web.read")
    if data.get("allow_write") is True:
        requested.add("project.write")
    web_via_task_text = intent in {"project", "hybrid", "auto", "chat"} and explicit_web_intent(task).requested
    if web_via_task_text:
        requested.add("web.read")
    strict = data.get("strict_research") is True or intent == "research"
    requires = derive_project_changes_required(data, intent=intent, project=project)
    if requires and _explicit_readonly_task(task) and data.get("project_changes_required") is not True:
        requires = False
    explicit_sources = data.get("sources_open_required")
    if explicit_sources is True:
        sources_required = True
    elif explicit_sources is False:
        sources_required = False
    else:
        # “可以”不等于“必须”：仅任务文本明确需要来源时才必须打开
        sources_required = (web_via_task_text and intent in {"auto", "project", "hybrid", "chat"})
    denied: set[str] = set()
    if data.get("allow_web") is False:
        denied.add("web.read")
        sources_required = False
    if intent in {"project", "hybrid", "auto", "chat"} and _explicit_readonly_task(task):
        # 明确只读：取消“必须修改”，同时落实“禁止修改”。project_changes_required
        # 只表示完成要求，写权限由 denied_capabilities 独立否决。
        denied.update({"project.write", "shell.approval"})
    requested = {item for item in requested if item not in denied}
    return EntryAuth(
        requested_capabilities=tuple(sorted(requested)),
        strict_research=strict,
        sources_open_required=sources_required,
        project_changes_required=requires,
        denied_capabilities=tuple(sorted(denied)),
    )
