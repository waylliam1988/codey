"""Pure prompt text for the shared task kernel (no task state).

Initial prompts, tool-result prompts, coding context, and protocol repair
prompts. All functions here build text from the given session snapshot data;
they never send, execute, or hold task state.
"""

from __future__ import annotations

from typing import Any

from codey.operations.task_session import TaskSession
from codey.runtime.core.models import ToolResult
from codey.workspace.coding_context import CodingContext, render_coding_context

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
    task_guidance: str = "",
    coding_context: CodingContext | None = None,
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
    if coding_context is not None:
        current = render_coding_context(coding_context, native=native)
        if current:
            parts.append(current)
    if task_guidance:
        parts.append(task_guidance)
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
