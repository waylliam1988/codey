"""User-entry authorization shared by HTTP and CLI/headless."""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from codey.policies.capabilities import BROWSER_CAPABILITIES, KNOWN_TASK_GRANTS
from codey.task.instruction_text import instruction_text
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
    inferred_intent: str = ""


_READONLY_MUST_NOT_CHANGE_MARKERS = (
    "不要修改", "不要改", "不修改", "不改动", "只检查", "只读", "不要改动",
    "read-only", "readonly", "do not modify", "don't modify", "do not change", "don't change",
    "do not edit", "don't edit", "must not modify", "must not change", "must not edit",
)
_NAMED_PRESERVATION_TARGET = (
    r"(?:(?:the|existing|original)\s+)*(?:tests\b|test suite\b|测试(?:文件|脚本|用例)?|"
    r"[\w/-]+\.(?:py|js|ts|tsx|jsx|json|md|txt)(?![a-z0-9_]))"
)
_OTHER_FILES = r"\bother\b.*\bfiles?\b|(?:其他|其它|其余).{0,16}(?:文件|代码|源码|项目)"
_ALL_FILES = r"\b(?:any|all)\s+(?:(?:project|source|code)\s+)?files?\b|(?:任何|所有|全部).{0,16}(?:文件|代码|源码|项目)"
_EDIT_REQUEST = re.compile(
    r"(?:^|[，。；;,.\n]|\bthen\b|然后|接着)\s*(?:请|please\s+)?"
    r"(?:修改|编辑|更新|修复|改代码|(?:modify|edit|update|fix|patch)\b)",
    re.IGNORECASE,
)


def _named_edit_scope(task: str) -> str:
    # Recognize an explicit allowance, not an arbitrary inferred task intent.
    # In that context 'other files' excludes the named editable target.
    match = re.search(
        r"(?:^|[;；。！？\n，]|[.!?]\s+)\s*(?:please\s+)?"
        r"(?:(?:change|modify|edit)\s+only\s+|only\s+(?:change|modify|edit)\s+|"
        r"(?:只|仅)(?:允许)?(?:修改|改动|编辑)\s*)(?P<target>" + _NAMED_PRESERVATION_TARGET + ")",
        task,
    )
    return re.sub(r"^(?:(?:the|existing|original)\s+)+", "", match.group("target")) if match is not None else ""


def _constraint_fragment(text: str, *, preceding: bool = False) -> str:
    pattern = r"[!?;。！？；\n]|\.(?=\s|$)"
    boundaries = list(re.finditer(pattern + (r"|[，,]" if preceding else ""), text))
    if not boundaries:
        return text
    return text[boundaries[-1].end():] if preceding else text[:boundaries[0].start()]


def _explicit_readonly_task(task: str) -> bool:
    lowered = (task or "").lower()
    named_edit_scope = _named_edit_scope(lowered)
    for marker in _READONLY_MUST_NOT_CHANGE_MARKERS:
        for match in re.finditer(re.escape(marker.lower()), lowered):
            suffix = lowered[match.end():].lstrip()
            # A request to preserve tests or one named file is a local task
            # constraint, not a denial of every implementation edit.
            scoped = re.match(_NAMED_PRESERVATION_TARGET, suffix)
            preceding = re.search(_NAMED_PRESERVATION_TARGET + r"\s*$", lowered[:match.start()])
            preservation = (marker.endswith((" modify", " change", " edit"))
                            or marker in {"不要修改", "不要改", "不要改动", "不修改", "不改动"})
            clause = _constraint_fragment(suffix)
            prefix = _constraint_fragment(lowered[:match.start()], preceding=True)
            targets = clause + " " + (preceding.group() if preceding is not None else prefix)
            if (preservation and named_edit_scope and not re.search(_ALL_FILES, clause)
                    and named_edit_scope not in targets
                    and re.search(_OTHER_FILES, clause + " " + prefix)):
                continue
            if preservation and (scoped or preceding):
                remainder = _constraint_fragment(suffix[scoped.end():] if scoped else suffix)
                if not re.search(_ALL_FILES + "|" + _OTHER_FILES, remainder):
                    continue
            return True
    return False


def derive_entry_auth(body: Mapping[str, object] | None, *, project: str | None = None) -> EntryAuth:
    """Derive entry auth from user submission only; never from model output.

    权限回答“可以做什么”，完成要求回答“必须做什么”：
    - Chat/auto 默认允许使用 Web 工具，由当前模型选择是否调用；
    - 显式 allow_web=False 优先于默认许可和 requested；
    - 联网许可不产生必须打开来源的义务；
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
        if text in KNOWN_TASK_GRANTS:
            requested.add(text)
    allow_web = data.get("allow_web")
    if allow_web is True or allow_web is None and intent in {"chat", "auto"}:
        requested.add("web.read")
    if data.get("allow_write") is True:
        requested.add("project.write")
    strict = data.get("strict_research") is True or intent == "research"
    readonly = _explicit_readonly_task(instruction_text(task))
    infer_hybrid = (
        intent == "auto" and bool(project) and not strict and not readonly and allow_web is not False
        and data.get("input_kind") not in ("resume_run", "resolve_shell_approval")
        and data.get("continue_task") is not True
        and data.get("project_changes_required") is not False
        and explicit_web_intent(task, source_lines=False).requested
        and (data.get("project_changes_required") is True or _EDIT_REQUEST.search(instruction_text(task)) is not None)
    )
    requires = derive_project_changes_required(data, intent="hybrid" if infer_hybrid else intent, project=project)
    if requires and readonly and data.get("project_changes_required") is not True:
        requires = False
    sources_required = data.get("sources_open_required") is True
    denied: set[str] = set()
    if allow_web is False:
        denied.update({"web.read", *BROWSER_CAPABILITIES})
        sources_required = False
    if intent in {"project", "hybrid", "auto", "chat"} and readonly:
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
        inferred_intent="hybrid" if infer_hybrid else "",
    )
