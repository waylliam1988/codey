"""User-entry authorization shared by HTTP and CLI/headless."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from codey.task.model import derive_project_changes_required


@dataclass(frozen=True)
class EntryAuth:
    """User-authorized capabilities derived once at the submission boundary."""

    requested_capabilities: tuple[str, ...] = ()
    strict_research: bool = False
    project_changes_required: bool = False
    sources_open_required: bool = False
    denied_capabilities: tuple[str, ...] = ()


_WEB_TASK_MARKERS = (
    "web_search", "open_url", "查网页", "查资料", "官方文档",
    "浏览网页", "联网查询", "在线查找", "读取网页",
)

_WEB_NEGATION_MARKERS = (
    "不要", "勿", "禁止", "无需", "不需要", "不用",
    "do not", "don't", "never", "without", "no web", "offline only",
)

_READONLY_MUST_NOT_CHANGE_MARKERS = (
    "不要修改", "不要改", "不修改", "不改动", "只检查", "只读", "不要改动",
    "read-only", "readonly", "do not modify", "don't modify", "do not change",
)

_CLAUSE_SPLIT_CHARS = ("，", "。", "；", "、", ",", ".", ";", "!", "?", "！", "？", "\n")

_KNOWN_ENTRY_GRANTS = frozenset({
    "project.read", "project.write", "project.verify", "shell.approval",
    "web.read", "knowledge.read", "knowledge.write", "knowledge.link", "control",
})


def _clause_for_position(text: str, position: int) -> str:
    start = 0
    for idx, ch in enumerate(text):
        if ch in _CLAUSE_SPLIT_CHARS and idx < position:
            start = idx + 1
    end = len(text)
    for idx in range(position, len(text)):
        if text[idx] in _CLAUSE_SPLIT_CHARS:
            end = idx
            break
    return text[start:end]


def _explicit_readonly_task(task: str) -> bool:
    lowered = str(task or "").lower()
    for marker in _READONLY_MUST_NOT_CHANGE_MARKERS:
        for match in re.finditer(re.escape(marker.lower()), lowered):
            suffix = lowered[match.end():].lstrip()
            # A request to preserve tests or one named file is a local task
            # constraint, not a denial of every implementation edit.
            scoped = re.match(
                r"(?:(?:the|existing|original)\s+)*(?:tests\b|test suite\b|[\w/-]+\.(?:py|js|ts|tsx|jsx|json|md|txt)\b)",
                suffix,
            )
            if marker in {"do not modify", "don't modify", "do not change"} and scoped:
                remainder = re.split(r"[.!?;\n]", suffix[scoped.end():], maxsplit=1)[0]
                if not re.search(r"\b(?:any|all|other)\b.*\bfiles?\b", remainder):
                    continue
            return True
    return False


def derive_entry_auth(body: dict[str, Any] | None, *, project: str | None = None) -> EntryAuth:
    """Derive entry auth from user submission only; never from model_hint.

    权限回答“可以做什么”，完成要求回答“必须做什么”：
    - 宽泛“查一下”不再视为联网授权，仅明确动作表达才授权；
    - 否定仅作用于同一分句的对应动作，不影响另一分句；
    - allow_web/requested 仅给“可以”，不自动等于“必须打开来源”；
    - 明确只读目标不生成必须修改要求。
    """
    data = body if isinstance(body, dict) else {}
    intent = str(data.get("intent") or "auto").strip().lower()
    task = str(data.get("task") or "")
    raw_requested = data.get("requested_capabilities", ())
    if isinstance(raw_requested, str):
        raw_requested = (raw_requested,)
    try:
        items = tuple(raw_requested or ())
    except TypeError:
        items = ()
    requested: set[str] = set()
    for item in items:
        text = str(item or "").strip().lower()
        if text in _KNOWN_ENTRY_GRANTS:
            requested.add(text)
    if data.get("allow_web") is True:
        requested.add("web.read")
    if data.get("allow_write") is True:
        requested.add("project.write")
    web_via_task_text = False
    if intent in {"project", "hybrid", "auto"}:
        lowered = task.lower()
        for marker in _WEB_TASK_MARKERS:
            marker_lower = marker.lower()
            position = lowered.find(marker_lower)
            if position < 0:
                continue
            # 否定只作用于同一分句：跨分句的否定不影响本 marker
            clause = _clause_for_position(lowered, position)
            if any(negation in clause for negation in _WEB_NEGATION_MARKERS):
                continue
            requested.add("web.read")
            web_via_task_text = True
            break
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
        sources_required = bool(web_via_task_text and intent in {"auto", "project", "hybrid"})
    denied: set[str] = set()
    if intent in {"project", "hybrid", "auto"} and _explicit_readonly_task(task):
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
