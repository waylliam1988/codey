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
    tool_names: Any = None,
) -> str:
    task_text = str(user_task or getattr(session, "task_text", "") or "").strip()
    handoff = str(getattr(session, "handoff", "") or "").strip()
    project = str(getattr(session, "project", "") or "").strip() or "-"
    visible_names = (
        tuple(str(name) for name in tool_names)
        if tool_names is not None
        else _snapshot_names(getattr(session, "policy", None), controller_allowed)
    )
    names = ", ".join(visible_names) or "none"
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
            "来源质量, 搜索覆盖, 来源. Cite only sources opened and saved in this run.\n\n"
            "Strict research completion checklist: Do not call done before knowledge_write "
            "has saved evidence from an opened source. After a completion rejection, use "
            "knowledge_read to inspect the saved evidence and repair the report before "
            "trying done again. Do not repeat knowledge_write for the same evidence; add "
            "a new evidence excerpt only when the opened source supports a new claim. "
            "For knowledge_read, pass the note id exactly as returned by knowledge_write; "
            "do not prepend facts/ or append .md.\n\n"
            "Report format is strict: use these literal Markdown headings, each on its own line: "
            "## 结论, ## 关键证据, ## 反证与限制, ## 来源质量, ## 搜索覆盖, ## 来源. "
            "Do not replace headings with bold labels or inline prose. Put [1]-style citations "
            "in 结论 and 关键证据; 反证与限制 must cite [n] or explicitly say 未找到强反证 "
            "and state what was searched. A valid no-counter example is exactly: "
            "- 未找到强反证；本轮仅检索并打开上述来源。[1]. In 来源, list each cited source as "
            "[1] Title - https://...; "
            "use only URLs opened and saved in this run."
        )
    parts.append(f"Visible tools: {names}")
    if contract_text:
        if native:
            parts.append(f"Tool contract (use exactly these tools):\n{contract_text}")
        else:
            parts.append(f"Tool contract (use exactly these shapes):\n{contract_text}")
    if native:
        parts.append(
            "Call exactly one native tool per turn and wait for its result before choosing the next tool. "
            "Call done separately, only after the required checks have passed. Do not reply with raw JSON."
        )
    else:
        parts.append(
            "Reply with exactly one JSON tool call per turn, or done. "
            r"Escape line breaks and tabs inside JSON strings as \n and \t. "
            r'For example: "content":"first line\nsecond line\n".'
        )
    return "\n\n".join(parts)


def _coding_context_for_session(session: TaskSession) -> str:
    if not bool(getattr(session, "coding_context_enabled", True)):
        return ""
    if getattr(session, "task_kind", "") not in {"project", "hybrid", "planning"}:
        return ""
    try:
        if not session.policy.allows("project.read"):
            return ""
        from codey.operations.project_completion_checks import project_completion_checks
        from codey.operations.project_verification import refresh_verification_candidates
        from codey.workspace.coding_context import CodingContext, render_coding_context

        refresh_verification_candidates(session)
        fresh = any(row.check_id == "relevant_verification" and row.status == "pass"
                    for row in project_completion_checks(session))
        return render_coding_context(
            CodingContext(
                read_files=tuple(sorted(getattr(session, "read_files", set()) or set())),
                edit_eligible_files=tuple(sorted(getattr(session, "read_files", set()) or set()))
                if session.policy.allows("project.write") else (),
                changed_files=tuple(sorted(getattr(session, "edited_files", {}).keys())),
                verification_fresh=fresh,
                verification_forbidden=getattr(session, "verification_forbidden", False) is True,
                selected_verification=getattr(session, "selected_verification", None),
            )
        )
    except Exception as exc:
        # Optional enrichment only: a render failure is an observable
        # diagnostic, never a permission-style failure. The task continues
        # with the base prompt.
        logger.warning("coding context unavailable: %s", str(exc)[:120])
        return ""


def _repair_prompt(error: str, *, contract_text: str, native: bool) -> str:
    intro = (
        "Your previous reply was not a valid tool call "
        f"({(error or 'invalid').strip()}). "
    )
    if native:
        instruction = "Call exactly one native tool from the contract below. Do not reply with raw JSON or prose."
    else:
        instruction = (
            'Reply with exactly one JSON object using {"tool":"ACTUAL_TOOL_NAME","args":{...}}. '
            "Use double-quoted JSON keys, no prose and no tool-call tags. "
            "The tool field must name an actual tool from the contract below, not the word 'tool'."
        )
    return (
        intro + instruction
        + " Resend the same intended call and keep its intended file contents."
        + f"\n\nCurrent authorized tool contract:\n{contract_text}"
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
