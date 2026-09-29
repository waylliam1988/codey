"""Pure prompt text for the shared task kernel (no task state).

Initial prompts, tool-result prompts, coding context, and protocol repair
prompts. All functions here build text from the given session snapshot data;
they never send, execute, or hold task state.
"""

from __future__ import annotations

import logging
from typing import Any

from codey.operations.task_session import TaskSession
from codey.runtime.core.models import ToolResult

logger = logging.getLogger(__name__)

__all__ = [
    "kernel_prompt_for_session",
]


def _snapshot_names(policy: Any, controller_allowed: Any = None) -> tuple[str, ...]:
    """Per-turn tool snapshot; fail-closed on build failure (no fallback).

    A snapshot failure must terminate the turn (controller_failure), never
    fall back to a policy-scope list that would show the model tools the
    current turn cannot execute.
    """
    try:
        from codey.toolchain.tool_spec import visible_tool_names_for_snapshot
    except Exception as exc:
        raise RuntimeError(f"tool snapshot unavailable: {exc}") from exc
    return tuple(visible_tool_names_for_snapshot(policy, controller_allowed))


def kernel_prompt_for_session(
    session: TaskSession,
    *,
    user_task: str = "",
    contract_text: str = "",
    context_text: str = "",
    controller_allowed: Any = None,
    native: bool = False,
) -> str:
    task_text = str(user_task or getattr(session, "task_text", "") or "").strip()
    handoff = str(getattr(session, "handoff", "") or "").strip()
    project = str(getattr(session, "project", "") or "").strip() or "-"
    names = ", ".join(_snapshot_names(getattr(session, "policy", None), controller_allowed)) or "none"
    parts = [
        f"User task (verbatim):\n{task_text or '(no task text)'}\n",
        f"Project: {project}\nTask kind: {getattr(session, 'task_kind', '')}",
    ]
    if handoff:
        parts.append(f"Factual handoff from prior work:\n{handoff}")
    if context_text:
        parts.append(f"Project context:\n{context_text}")
    current = _coding_context_for_session(session)
    if current:
        parts.append(current)
    if bool(getattr(getattr(session, "policy", None), "strict_research", False)):
        parts.append(
            "Research evidence rules: search results are leads, not evidence. "
            "Use web_search, then open_result or open_url before citing a source. "
            "Save short exact excerpts with knowledge_write. Use only local tool "
            "results as evidence, including when a web model has built-in browsing. "
            "Finish with done and these report sections: 结论, 关键证据, 反证与限制, "
            "来源质量, 搜索覆盖, 来源. Cite only sources opened and saved in this run."
        )
    parts.append(f"Visible tools: {names}")
    if contract_text:
        if native:
            parts.append(f"Tool contract (use exactly these tools):\n{contract_text}")
        else:
            parts.append(f"Tool contract (use exactly these shapes):\n{contract_text}")
    if native:
        parts.append("Use the provided native tools for this turn; do not reply with raw JSON.")
    else:
        parts.append("Reply with exactly one JSON tool call per turn, or done.")
    return "\n\n".join(parts)


def _coding_context_for_session(session: TaskSession) -> str:
    if not bool(getattr(session, "coding_context_enabled", True)):
        return ""
    if getattr(session, "task_kind", "") not in {"project", "hybrid", "planning"}:
        return ""
    try:
        from codey.workspace.coding_context import CodingContext, render_coding_context

        return render_coding_context(
            CodingContext(
                read_files=tuple(sorted(getattr(session, "read_files", set()) or set())),
                edit_eligible_files=tuple(sorted(getattr(session, "read_files", set()) or set())),
                changed_files=tuple(sorted(getattr(session, "edited_files", {}).keys())),
                verification_fresh=bool(getattr(session, "verifications", ())),
            )
        )
    except Exception as exc:
        # Optional enrichment only: a render failure is an observable
        # diagnostic, never a permission-style failure. The task continues
        # with the base prompt.
        logger.warning("coding context unavailable: %s", str(exc)[:120])
        return ""


def _repair_prompt(error: str) -> str:
    return (
        "Your previous reply was not a valid tool call "
        f"({(error or 'invalid').strip()}). Reply with exactly one JSON object "
        'using {"tool":"...","args":{...}} and no other text.'
    )


def _result_context(result: ToolResult, session: TaskSession) -> str:
    text = str(result.model_text or "")
    if result.call.name == "web_search" and session.search_results:
        refs = "\n".join(f"{key}: {url}" for key, url in list(session.search_results.items())[-12:])
        return f"{text}\n\nAvailable result IDs for open_result:\n{refs}"
    if result.call.name == "source_search" and session.hit_targets:
        refs = "\n".join(
            f"{key}: {target.get('url')} offset={target.get('offset')} pages={target.get('pages')}"
            for key, target in list(session.hit_targets.items())[-12:]
        )
        return f"{text}\n\nAvailable hit IDs for open_hit:\n{refs}"
    if result.call.name in {"open_url", "open_result", "open_hit", "reopen_source"} and session.source_ids:
        refs = "\n".join(f"{key}: {url}" for key, url in list(session.source_ids.items())[-12:])
        return f"{text}\n\nOpened source IDs for reopen_source and citations:\n{refs}"
    return text


def _format_results(results: list[ToolResult], session: TaskSession) -> str:
    if not results:
        return "[no tool output]\n\nContinue with the next single JSON tool call."
    blocks = []
    for result in results:
        label = str(getattr(result.call, "name", "") or "tool")
        blocks.append(f"[result: {label}]\n{_result_context(result, session)}".rstrip())
    return "\n\n".join(blocks) + "\n\nContinue with the next single JSON tool call, or done."
