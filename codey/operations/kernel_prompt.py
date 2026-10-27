"""Pure prompt text for the shared task kernel (no task state).

Initial prompts, tool-result prompts, coding context, and protocol repair
prompts. All functions here build text from the given session snapshot data;
they never send, execute, or hold task state.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

from codey.operations.task_session import TaskSession
from codey.policies.command_line import COMMAND_WORKDIR_DESCRIPTION
from codey.providers.base import render_result_metadata
from codey.runtime.core.models import ToolPlan, ToolResult
from codey.utils.refs import strict_exit_code, strict_verification_success
from codey.workspace.coding_context import CodingContext, render_coding_context
from codey.workspace.revision import valid_workspace_fingerprint, valid_workspace_revision

if TYPE_CHECKING:
    from codey.operations.kernel_protocol import TurnSnapshot

__all__ = [
    "kernel_prompt_for_session",
]

_NATIVE_ARGUMENTS_RULE = (
    "Pass schema properties directly as native arguments; omit the text protocol's tool/args envelope."
)
_NATIVE_READ_BATCH_RULE = (
    "Group independent read-only calls into one native tool_calls turn. "
    "Wait for their results before dependent actions. Submit verification and done separately."
)
_NATIVE_FINAL_RULE = (
    "For the final answer, use done if declared: put the complete response in its summary string. "
    "Preserve the user's requested final format inside that string. "
    "Requested JSON is a bare document without Markdown fences inside summary, not the outer tool-call object."
)


def kernel_prompt_for_session(
    session: TaskSession,
    *,
    tool_names: tuple[str, ...],
    user_task: str = "",
    contract_text: str = "",
    context_text: str = "",
    native: bool = False,
    task_guidance: str = "",
    coding_context: CodingContext | None = None,
) -> str:
    task_text = str(user_task or getattr(session, "task_text", "") or "").strip()
    handoff = str(getattr(session, "handoff", "") or "").strip()
    project = str(getattr(session, "project", "") or "").strip() or "-"
    visible_names = tool_names
    names = ", ".join(visible_names) or "none"
    parts = [
        f"User task (verbatim):\n{task_text or '(no task text)'}\n",
        f"Workspace: {project}\nTask kind: {getattr(session, 'task_kind', '')}",
    ]
    if handoff:
        parts.append(f"Factual handoff from prior work:\n{handoff}")
    if context_text:
        parts.append(f"Task context:\n{context_text}")
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
            "For actions, use native tool calls, not JSON in prose. "
            + _NATIVE_READ_BATCH_RULE + " " + _NATIVE_ARGUMENTS_RULE + " " + _NATIVE_FINAL_RULE
        )
    else:
        parts.append(
            "Answer directly when no action is needed. For actions, reply with exactly one bare JSON tool call "
            "per turn and wait for its result. Do not wrap executable calls in code fences. "
            "Deliver the final answer as normal text or call done. "
            r"Escape line breaks and tabs inside JSON strings as \n and \t. "
            r'For example: "content":"first line\nsecond line\n".'
        )
    parts.append(
        "Preserve the user's requested final format, including JSON if requested. "
        "If the task cannot be completed within authorized capabilities, report it as blocked with the reason; "
        "never claim a fix or passing verification that did not happen. For completed changes, required checks "
        "must pass on the current workspace before completion. Tool availability alone does not require edits or tests. "
        "Shell runs on the execution host and requires approval. A working directory is not a system sandbox."
    )
    parts.append(
        "For current external facts, obtain actual source data using the authorized tools instead of guessing. "
        "A CodeMode host-access rejection is local to that sandbox; it is not evidence that the host has no network access. "
        "Do not claim an environment-wide network or permission failure from a malformed call or one failed endpoint. "
        "Report the specific observed failure and consider a different authorized path."
    )
    if {"run", "shell"}.intersection(visible_names):
        parts.append(COMMAND_WORKDIR_DESCRIPTION)
    if "run" in visible_names:
        parts.append(
            "run executes allowed project verification commands directly as argv, without a shell. "
            "Use a single verification command, not pipes, redirections, environment assignments or shell expansion. "
            "Use output_tail_lines to limit displayed output. "
            "Other authorized commands require the existing shell approval workflow."
        )
    if "shell" in visible_names:
        parts.append(f"Execution host platform: {sys.platform}. " + session.shell.guidance())
        parts.append(
            "When a public URL or API can answer the request, shell can request a bounded noninteractive HTTP command "
            "such as curl.exe on Windows or curl elsewhere, with explicit connection and total timeouts. "
            "This does not open a browser. Keep shell approval intact; never put fetch, process or imports in CodeMode. "
            "Do not launch a browser as a fallback for an ordinary data lookup unless the user requests browser interaction. "
            "Use returned source and observation times when reporting time-sensitive facts."
        )
    return "\n\n".join(parts)


def _repair_prompt(plan: ToolPlan, *, snapshot: TurnSnapshot, native: bool) -> str:
    from codey.toolchain.constants import ARGUMENT_CONTENT_GUIDANCE
    from codey.toolchain.tool_prompt import tool_repair_text, tool_signature

    error = (plan.protocol_error or "invalid tool call").strip()
    spec = next((item for item in snapshot.frozen_specs if item.name == plan.protocol_tool_name), None)
    if native:
        detail = (error + f"\nCorrect {spec.name}; required arguments: {', '.join(spec.required) or '(none)'}."
                  if spec is not None else error + "\nAvailable tools: " + ", ".join(snapshot.tool_names))
        instruction = "Use the provided native tools. " + _NATIVE_ARGUMENTS_RULE
        if spec is None or spec.name == "done":
            instruction += " " + _NATIVE_FINAL_RULE
    else:
        detail = (tool_repair_text(error, spec) if spec is not None else
                  error + "\nAvailable tool signatures:\n" + "\n".join(tool_signature(item) for item in snapshot.frozen_specs))
        instruction = (
            'Reply with exactly one JSON object using {"tool":"ACTUAL_TOOL_NAME","args":{...}}. '
            "Use double-quoted JSON keys, no prose and no tool-call tags. "
            "The tool field must name an available tool, not the word 'tool'."
        )
    return f"Protocol error: {detail}\n\n{instruction} Resend the intended call. {ARGUMENT_CONTENT_GUIDANCE} Keep other valid fields."


def _completion_followup(feedback: str, *, native: bool) -> str:
    """Render completion feedback for its transport without changing the verdict."""
    return feedback + "\n\n" + _NATIVE_FINAL_RULE if native else feedback


def _result_context(result: ToolResult, session: TaskSession) -> str:
    text = (result.model_text or "")
    if result.call.name == "codemode" and result.ok:
        text = f"Result data (JSON):\n{text}"
    result_ref = next((key for key, value in session._memory_results.items() if value is result), "")
    if result_ref:
        instruction = (
            "For omitted detail, use read_tool_result with query (a literal keyword) or a bounded offset."
            if result.truncated else "If more detail is needed, use read_tool_result."
        )
        text += (f"\n\n{render_result_metadata(result_ref, tool=result.call.name, ok=result.ok)}\n"
                 "Use the complete result_ref string verbatim, including its prefix. "
                 f"{instruction} do not repeat execution to recover output.")
    if result.call.name == "web_search" and session.search_results:
        refs = "\n".join(f"{key}: {url}" for key, url in list(session.search_results.items())[-12:])
        return f"{text}\n\nSearch result references:\n{refs}"
    if result.call.name == "source_search" and session.hit_targets:
        refs = "\n".join(
            f"{key}: {target.get('url')} offset={target.get('offset')} pages={target.get('pages')}"
            for key, target in list(session.hit_targets.items())[-12:]
        )
        return f"{text}\n\nSource locator references:\n{refs}"
    if result.call.name in {"open_url", "open_result", "open_hit", "reopen_source"} and session.source_ids:
        refs = "\n".join(f"{key}: {url}" for key, url in list(session.source_ids.items())[-12:])
        return f"{text}\n\nOpened source references for citations:\n{refs}"
    return text


def _format_results(results: list[ToolResult], session: TaskSession) -> str:
    if not results:
        return "[no tool output]\n\nContinue with the next single JSON tool call."
    blocks = []
    for result in results:
        label = str(getattr(result.call, "name", "") or "tool")
        blocks.append(f"[result: {label}]\n{_result_context(result, session)}".rstrip())
    return "\n\n".join(blocks) + "\n\nContinue with one bare JSON tool call, or deliver the final answer."


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
