"""Pure prompt text for the shared task kernel (no task state).

Initial prompts, tool-result prompts, coding context, and protocol repair
prompts. All functions here build text from the given session snapshot data;
they never send, execute, or hold task state.
"""

from __future__ import annotations

from typing import Any

from codey.operations.task_session import TaskSession
from codey.runtime.core.models import ToolResult
from codey.utils.refs import strict_exit_code, strict_verification_success
from codey.workspace.coding_context import CodingContext, render_coding_context
from codey.workspace.revision import valid_workspace_fingerprint, valid_workspace_revision

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
            parts.append("Use the provided native tool schemas for tool names and arguments.")
        else:
            parts.append(f"Tool contract (use exactly these shapes):\n{contract_text}")
    if native:
        parts.append(
            "Call exactly one native tool per turn and wait for its result before choosing the next tool. "
            "Call done separately to deliver the final answer; do not send a raw JSON tool call or prose instead."
        )
    else:
        parts.append(
            "Reply with exactly one JSON tool call per turn, or done. "
            r"Escape line breaks and tabs inside JSON strings as \n and \t. "
            r'For example: "content":"first line\nsecond line\n".'
        )
    parts.append(
        "Preserve the user's requested final format inside done's summary string, including JSON if requested. "
        "If the task cannot be completed within authorized capabilities, report it as blocked with the reason; "
        "never claim a fix or passing verification that did not happen. For completed changes, required checks "
        "must pass on the current workspace before done."
    )
    return "\n\n".join(parts)


def _repair_prompt(error: str, *, contract_text: str, native: bool) -> str:
    intro = (
        "Your previous reply was not a valid tool call "
        f"({(error or 'invalid').strip()}). "
    )
    if native:
        return (
            intro + "Call exactly one of the provided native tools with schema-correct arguments. "
            "Do not emit text tool-call tags, JSON tool wrappers or prose. "
            "Resend the intended call with complete arguments and concise file contents."
        )
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
    result_ref = next((key for key, value in session._memory_results.items() if value is result), "")
    if result_ref:
        instruction = (
            "For omitted detail, use read_tool_result with query (a literal keyword) or a bounded offset."
            if result.truncated else "If more detail is needed, use read_tool_result."
        )
        text += f"\nStored result: {result_ref}\n{instruction} do not repeat execution to recover output."
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


def working_context(session: TaskSession) -> str:
    """Fresh execution facts; narrative checkpoints cannot amend these observations."""
    import json

    executions: list[dict[str, Any]] = []
    commands = [(identity, record) for identity, record in session.executed.items() if record.get("name") == "run"]
    for identity, record in commands[-12:]:
        result = session._memory_results.get(identity)
        args = result.call.args if result is not None else {}
        fingerprint = record.get("workspace_fingerprint")
        audit: dict[str, Any] = dict(result.audit) if result is not None else {}
        executions.append({"execution_ref": identity, "command": args.get("command", record.get("command", "")),
            "cwd": args.get("path", record.get("cwd", ".")),
            "status": ("denied_before_execution"
                       if record.get("execution_disposition") == "denied_before_execution"
                       else "complete" if type(record.get("exit_code")) is int else "unknown"),
            "started_at": audit.get("command_started_at"), "finished_at": audit.get("command_finished_at"),
            "exit_code": record.get("exit_code"), "result_available": result is not None,
            "workspace_identity": fingerprint,
            "verification": "current" if fingerprint and fingerprint == session.workspace_fingerprint else "stale or unknown"})
    verifications: dict[tuple[str, str], dict[str, Any]] = {}
    for row in session.verifications[-12:]:
        revision = valid_workspace_revision(row.get("workspace_revision"))
        fingerprint = valid_workspace_fingerprint(row.get("workspace_fingerprint"))
        code = strict_exit_code(row.get("exit_code"))
        state = "unknown"
        if revision and fingerprint and code is not None:
            if revision != session.workspace_revision or fingerprint != session.workspace_fingerprint:
                state = "stale"
            else:
                state = "current_pass" if strict_verification_success(row.get("passed"), code) else "current_fail"
        command, cwd = str(row.get("command", "")), str(row.get("cwd", "."))
        verifications[command, cwd] = {"command": command, "cwd": cwd, "state": state,
            "exit_code": code, "workspace_revision": revision, "workspace_fingerprint": fingerprint}
    can_modify = session.policy.allows("project.write") if session.policy is not None else None
    finish_guidance = (
        "For read-only work, report diagnostic findings in done's summary using the requested final format. "
        "If the requested fix requires forbidden changes, report blocked and why. "
        "Do not rerun unchanged checks to try to repair a defect. "
        if session.task_kind == "project" and can_modify is False else
        "If the user's requirements are satisfied and required verification is current and passing, call done. "
        "Do not change correct files merely to make progress. "
    )
    return ("Current work observations (runtime records):\n" + json.dumps({"user_task": session.task_text,
        "project": session.project, "workspace_revision": session.workspace_revision,
        "workspace_fingerprint": session.workspace_fingerprint, "changed_files": list(session.edited_files),
        "read_files": sorted(session.read_files),
        "executions": executions, "verifications": list(verifications.values()), "can_modify_project": can_modify}, ensure_ascii=False)
        + "\n" + finish_guidance + "Read stored results only when more detail is needed. "
          "Do not repeat an execution merely to recover its output. Stale results cannot prove current correctness.")
