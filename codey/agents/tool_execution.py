"""Tool policy, dispatch, and result accounting for the agent loop."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from codey.agents.protocol import (
    canonical_project_path,
    edit_blocks_from_call,
    edit_has_content,
)
from codey.agents.state import AgentLoopSession
from codey.agents.verification_driver import (
    record_edit_change,
    record_run_attempt,
)
from codey.policies.action import (
    DECISION_ASK_USER,
    DECISION_DENY,
    ActionPolicyDecision,
    ActionSubject,
    evaluate_action,
)
from codey.runtime.core.models import ToolCall
from codey.toolchain.definition import (
    INFORMATION_RUNTIME_TOOL_NAMES,
    SUPPORTED_RUNTIME_TOOL_NAMES,
    call_arg,
)
from codey.toolchain.runtime import ToolOutcome, safe_join

SUPPORTED_TOOL_NAMES = SUPPORTED_RUNTIME_TOOL_NAMES
INFORMATION_TOOL_NAMES = INFORMATION_RUNTIME_TOOL_NAMES

TOOL_OUTPUT_BUDGET_BYTES = 24_000
TOOL_OUTPUT_HEAD_BYTES = 12_000
TOOL_OUTPUT_TAIL_BYTES = 8_000


def _head_tail_clip(text: str) -> tuple[str, int]:
    raw = text.encode("utf-8")
    raw_bytes = len(raw)
    head = raw[:TOOL_OUTPUT_HEAD_BYTES].decode("utf-8", errors="ignore")
    tail = raw[-TOOL_OUTPUT_TAIL_BYTES:].decode("utf-8", errors="ignore") if len(raw) > TOOL_OUTPUT_HEAD_BYTES else ""
    receipt = (
        f"\n[... output externalized: {raw_bytes} bytes; showing head/tail. "
        "Use narrower offsets for the rest.]\n"
    )
    return (head + receipt + tail if tail else head + receipt), raw_bytes


def maybe_externalize_large_tool_output(
    session: AgentLoopSession,
    call: ToolCall,
    outcome: ToolOutcome,
    *,
    turn: int,
    tool_index: int,
) -> ToolOutcome:
    """Bound oversized model text; persist the full text when a store exists.

    With ``session.request.managed_outputs`` wired (project runs), the full
    output is stored durably and the model sees a head/tail receipt plus a
    ``managed_output`` audit handle. Without a store (unit tests, ad-hoc
    loops) it degrades to the same inline head/tail clip. Never changes
    execution semantics.
    """

    text = str(outcome.model_text or "")
    raw_bytes = len(text.encode("utf-8"))
    if raw_bytes <= TOOL_OUTPUT_BUDGET_BYTES and not outcome.truncated:
        return outcome
    if outcome.managed_output():
        return outcome
    audit: dict[str, object] = dict(outcome.audit)
    store = session.request.managed_outputs
    if store is not None and session.request.session_id and session.request.run_id:
        try:
            ref = store.write_tool_output(
                session_id=session.request.session_id,
                run_id=session.request.run_id,
                tool_id=f"{turn}:{tool_index}",
                permission_profile=session.config.profile.name,
                tool_name=call.name,
                display_ref=call_arg(call, "path", call_arg(call, "command", "")),
                text=text,
            )
            failure = "" if ref is not None else "store_refused"
        except Exception as exc:
            ref = None
            failure = type(exc).__name__ or "store_error"
        if ref is None:
            audit["managed_output_failed"] = True
            audit["managed_output_failure"] = failure or "store_refused"
        else:
            clipped, _ = _head_tail_clip(text)
            audit["managed_output"] = {
                "handle": ref.handle,
                "original_bytes": ref.original_bytes,
                "stored_bytes": ref.stored_bytes,
                "sha256": ref.sha256,
                "original_sha256": ref.original_sha256,
                "stored_truncated": ref.stored_truncated,
            }
            return ToolOutcome(
                clipped,
                outcome.ok,
                canonical=dict(outcome.canonical),
                presentation=dict(outcome.presentation),
                audit={**audit, "externalized": True, "original_bytes": raw_bytes},
                error_code=outcome.error_code,
                exit_code=outcome.exit_code,
                changed=outcome.changed,
                truncated=True,
            )
    clipped, _ = _head_tail_clip(text)
    return ToolOutcome(
        clipped,
        outcome.ok,
        canonical=dict(outcome.canonical),
        presentation=dict(outcome.presentation),
        audit={**audit, "externalized": True, "original_bytes": raw_bytes},
        error_code=outcome.error_code,
        exit_code=outcome.exit_code,
        changed=outcome.changed,
        truncated=True,
    )


def action_subject_for_call(
    call: ToolCall,
    *,
    project: Path,
    permission_profile: str,
    phase: str,
    approval_available: bool = False,
) -> ActionSubject | None:
    path = call_arg(call, "path", ".")
    if call.name == "read":
        kind = "read_file"
    elif call.name == "ls":
        kind = "list_dir"
    elif call.name == "search":
        kind = "search_files"
    elif call.name == "references":
        kind = "find_references"
    elif call.name == "edit":
        kind = "write_file" if edit_has_content(call) else "edit_file"
    elif call.name == "run":
        kind = "run_command"
    elif call.name == "shell":
        kind = "shell"
    else:
        kind = "unknown_tool"
    return ActionSubject(
        kind=kind,
        phase=phase,
        permission_profile=permission_profile,
        project=str(project),
        path=path,
        command=call_arg(call, "command"),
        tool_name=call.name,
        approval_available=approval_available,
    )


def evaluate_tool_call_policy_for(
    call: ToolCall,
    *,
    project: Path,
    permission_profile: str,
    approval_available: bool = False,
    phase: str = "writer",
) -> tuple[ActionPolicyDecision | None, Any]:
    policy_subject = action_subject_for_call(
        call,
        project=project,
        permission_profile=permission_profile,
        phase=phase,
        approval_available=approval_available,
    )
    policy_decision = (
        evaluate_action(policy_subject)
        if policy_subject is not None
        else None
    )
    from codey.runtime.effects.replay_policy import tool_replay_policy
    is_denied = policy_denied(policy_decision)
    is_approval = (call.name == "shell") and policy_asks_user(policy_decision)
    replay_decision = tool_replay_policy(
        call.name,
        policy_denied=is_denied,
        approval_required=is_approval,
    )
    return policy_decision, replay_decision


def policy_denied(decision: ActionPolicyDecision | None) -> bool:
    return decision is not None and decision.decision == DECISION_DENY


def policy_asks_user(decision: ActionPolicyDecision | None) -> bool:
    return decision is not None and decision.decision == DECISION_ASK_USER


def read_before_edit_outcome(
    root: Path,
    rel: str,
    known_file_paths: set[str],
) -> ToolOutcome | None:
    try:
        canonical = canonical_project_path(root, rel)
        target = safe_join(root, canonical)
    except ValueError:
        return ToolOutcome.error("workspace_escape: path escapes project root")
    if target.is_file() and canonical not in known_file_paths:
        return ToolOutcome.error(
            f"read_file required before editing existing file: {canonical}"
        )
    return None


def execute_edit_call(session: AgentLoopSession, call: ToolCall) -> ToolOutcome:
    path = call_arg(call, "path", ".")
    if edit_has_content(call):
        canonical = canonical_project_path(session.config.project, path)
        if safe_join(session.config.project, canonical).is_file():
            outcome = ToolOutcome.error(
                "content is only allowed when creating a new file; "
                f"use replacements for existing file: {canonical}"
            )
        else:
            if session.request.change_tracker is not None:
                session.request.change_tracker.capture_before(path)
            outcome = session.config.tool_fns.write_file(
                session.config.project,
                path,
                call_arg(call, "content"),
            )
    else:
        guard = read_before_edit_outcome(
            session.config.project,
            path,
            session.progress.known_file_paths,
        )
        if guard is not None:
            outcome = guard
        else:
            if session.request.change_tracker is not None:
                session.request.change_tracker.capture_before(path)
            outcome = session.config.tool_fns.edit_file(
                session.config.project,
                path,
                edit_blocks_from_call(call),
            )
    if outcome.ok and outcome.changed:
        if session.request.change_tracker is not None:
            session.request.change_tracker.capture_after(path)
        record_edit_change(
            session,
            canonical_project_path(session.config.project, path),
        )
    return outcome


def execute_run_call(
    session: AgentLoopSession,
    call: ToolCall,
    *,
    turn: int,
    tool_index: int,
) -> ToolOutcome:
    path = call_arg(call, "path", ".")
    command = call_arg(call, "command")
    outcome = session.config.tool_fns.execute_run_command(
        session.config.project,
        path,
        command,
        permission_profile=session.config.profile.name,
        phase="writer",
        tool_id=f"{turn}:{tool_index}",
    )
    record_run_attempt(
        session,
        command=command,
        path=path,
        ok=outcome.ok,
    )
    return outcome


def execute_information_tool_call(
    project: Path,
    tool_fns: Any,
    call: ToolCall,
) -> ToolOutcome:
    """Execute a pure read/information tool call in a replay-safe manner."""
    path = call_arg(call, "path", ".")
    if call.name == "read":
        read_options = {
            name: call.args[name]
            for name in ("offset", "limit")
            if name in call.args
        }
        return tool_fns.read_file(project, path, **read_options)
    if call.name == "ls":
        return tool_fns.list_directory(project, path)
    if call.name == "search":
        search_options = {
            name: call.args[name]
            for name in ("offset", "limit")
            if name in call.args
        }
        return tool_fns.search_files(
            project,
            path,
            call_arg(call, "query"),
            **search_options,
        )
    if call.name == "references":
        return tool_fns.find_references(
            project,
            path,
            call_arg(call, "symbol"),
        )
    return ToolOutcome.error(f"unsupported information tool {call.name} (path={path})")


__all__ = [
    "INFORMATION_TOOL_NAMES",
    "SUPPORTED_TOOL_NAMES",
    "action_subject_for_call",
    "call_arg",
    "evaluate_tool_call_policy_for",
    "execute_edit_call",
    "execute_information_tool_call",
    "execute_run_call",
    "maybe_externalize_large_tool_output",
    "policy_asks_user",
    "policy_denied",
    "read_before_edit_outcome",
]
