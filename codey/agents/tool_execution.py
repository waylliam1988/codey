"""Tool policy, dispatch, and result accounting for the agent loop."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codey.agents.protocol import (
    canonical_project_path,
    edit_blocks_from_call,
    edit_has_content,
)
from codey.agents.state import AgentLoopSession, emit
from codey.agents.verification_driver import (
    mark_policy_denied_run,
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
from codey.runtime.events import RunEvent
from codey.runtime.models import ToolCall, ToolResult
from codey.toolchain.definition import (
    INFORMATION_RUNTIME_TOOL_NAMES,
    SUPPORTED_RUNTIME_TOOL_NAMES,
    render_tool_activity,
)
from codey.toolchain.runtime import ToolOutcome, safe_join


SUPPORTED_TOOL_NAMES = SUPPORTED_RUNTIME_TOOL_NAMES
INFORMATION_TOOL_NAMES = INFORMATION_RUNTIME_TOOL_NAMES


@dataclass
class TurnState:
    results: list[ToolResult] = field(default_factory=list)
    made_progress: bool = False


def call_arg(call: ToolCall, name: str, default: str = "") -> str:
    value = call.args.get(name, default)
    if value is None:
        return default
    return str(value)


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
        return None
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
    from codey.runtime.replay_policy import tool_replay_policy
    is_denied = policy_denied(policy_decision)
    is_approval = (call.name == "shell") and policy_asks_user(policy_decision)
    replay_decision = tool_replay_policy(
        call.name,
        policy_denied=is_denied,
        approval_required=is_approval,
    )
    return policy_decision, replay_decision


def evaluate_tool_call_policy(
    session: AgentLoopSession,
    call: ToolCall,
    *,
    turn: int,
    tool_index: int,
) -> tuple[ActionPolicyDecision | None, Any]:
    policy_decision, replay_decision = evaluate_tool_call_policy_for(
        call,
        project=session.project,
        permission_profile=session.profile.name,
        phase="writer",
        approval_available=bool(session.on_shell_request),
    )
    if policy_decision is not None:
        session.trace.call("record_policy_decision", policy_decision)
    return policy_decision, replay_decision


def record_tool_call_intent(
    session: AgentLoopSession,
    call: ToolCall,
    *,
    turn: int,
    tool_index: int,
    replay_decision: Any,
) -> str:
    effects = session.runtime_effects
    if effects is None or not session.session_id or not session.run_id:
        return ""
    from codey.runtime.effect_records import (
        EFFECT_CATEGORY_TOOL_CALL,
        RuntimeEffectIntent,
        compute_args_digest,
        new_effect_id,
    )
    from codey.runtime.replay_policy import ReplayClass, is_replayable_safe_tool
    from codey.runtime.safe_tool_replay import replay_args_for_tool_call

    effect_id = new_effect_id(EFFECT_CATEGORY_TOOL_CALL, session.run_id)
    display_ref = call_arg(call, "path", ".") if call.name != "run" else call_arg(call, "command", "")
    replay_class = getattr(replay_decision, "replay_class", "unsafe")
    replay_args = (
        replay_args_for_tool_call(call)
        if replay_class == ReplayClass.SAFE and is_replayable_safe_tool(call.name)
        else None
    )
    intent = RuntimeEffectIntent(
        effect_id=effect_id,
        effect_category=EFFECT_CATEGORY_TOOL_CALL,
        session_id=session.session_id,
        run_id=session.run_id,
        phase="writer",
        turn=turn,
        tool_index=tool_index,
        tool_name=call.name,
        tool_id=f"{turn}:{tool_index}",
        display_ref=display_ref[:100],
        args_digest=compute_args_digest(call.args),
        replay_class=replay_class,
        replay_args=replay_args,
    )
    effects.record_intent(session.session_id, session.run_id, intent)
    return effect_id


def record_tool_call_settlement(
    session: AgentLoopSession,
    effect_id: str,
    *,
    outcome: ToolOutcome,
    replay_decision: Any,
) -> None:
    effects = session.runtime_effects
    if effects is None or not effect_id or not session.session_id or not session.run_id:
        return
    from codey.runtime.effect_records import (
        EFFECT_CATEGORY_TOOL_CALL,
        RuntimeEffectSettlement,
        SETTLEMENT_STATUS_ERROR,
        SETTLEMENT_STATUS_OK,
        record_settlement_safely,
    )
    status = SETTLEMENT_STATUS_OK if outcome.ok else SETTLEMENT_STATUS_ERROR
    error_code = str(outcome.error_code or ("" if outcome.ok else "error"))
    replay_class = getattr(replay_decision, "replay_class", "unsafe")
    settlement = RuntimeEffectSettlement(
        effect_id=effect_id,
        effect_category=EFFECT_CATEGORY_TOOL_CALL,
        session_id=session.session_id,
        run_id=session.run_id,
        status=status,
        error_code=error_code[:80],
        replay_class=replay_class,
    )
    record_settlement_safely(effects, session.session_id, session.run_id, settlement)


def emit_tool_started_after_intent(
    session: AgentLoopSession,
    call: ToolCall,
    *,
    turn: int,
    tool_index: int,
) -> None:
    if call.name != "shell":
        emit(
            session,
            RunEvent.tool_started(
                turn,
                call,
                render_tool_activity(call),
                index=tool_index,
            ),
        )


def policy_denied(decision: ActionPolicyDecision | None) -> bool:
    return decision is not None and decision.decision == DECISION_DENY


def policy_asks_user(decision: ActionPolicyDecision | None) -> bool:
    return decision is not None and decision.decision == DECISION_ASK_USER


def policy_error_outcome(decision: ActionPolicyDecision) -> ToolOutcome:
    message = decision.display or "action denied by policy"
    text = message if message.startswith("ERROR:") else f"ERROR: {message}"
    return ToolOutcome(
        text,
        False,
        presentation={"status": "error", "result": text.removeprefix("ERROR: ")[:200]},
        audit={"error_code": "policy_denied", "policy_decision": decision.to_audit_payload()},
        error_code="policy_denied",
    )


def tool_error_outcome(exc: BaseException) -> ToolOutcome:
    return ToolOutcome.error(str(exc))


def request_shell_approval(
    session: AgentLoopSession,
    *,
    path: str,
    command: str,
    policy_decision: ActionPolicyDecision | None,
) -> None:
    if policy_asks_user(policy_decision) and session.on_shell_request:
        session.on_shell_request(path, command)
    emit(
        session,
        RunEvent.status(
            f"[agent] shell approval requested: {command}"
        ),
    )


def read_before_edit_outcome(
    root: Path,
    rel: str,
    known_file_paths: set[str],
) -> ToolOutcome | None:
    canonical = canonical_project_path(root, rel)
    target = safe_join(root, canonical)
    if target.is_file() and canonical not in known_file_paths:
        return ToolOutcome.error(
            f"read_file required before editing existing file: {canonical}"
        )
    return None


def tool_result_from_outcome(call: ToolCall, outcome: ToolOutcome) -> ToolResult:
    return ToolResult(
        call=call,
        model_text=outcome.model_text,
        truncated=outcome.truncated,
        presentation=outcome.presentation,
        audit=outcome.audit,
        canonical=outcome.canonical,
    )


def record_tool_outcome(
    session: AgentLoopSession,
    turn_state: TurnState,
    *,
    turn: int,
    call: ToolCall,
    outcome: ToolOutcome,
    tool_index: int,
) -> None:
    path = call_arg(call, "path", ".")
    model_text = outcome.model_text
    emit(session, RunEvent.tool_finished(turn, call, outcome, index=tool_index))
    turn_state.results.append(tool_result_from_outcome(call, outcome))
    if call.name == "read" and outcome.ok:
        canonical = canonical_project_path(session.project, path)
        session.progress.read_file_paths.add(canonical)
        session.progress.known_file_paths.add(canonical)
    if call.name == "edit" and outcome.ok and outcome.changed:
        turn_state.made_progress = True
    produced_information = outcome.ok or outcome.exit_code is not None
    if call.name in INFORMATION_TOOL_NAMES and produced_information:
        sig = (call.name, path, model_text)
        if sig not in session.stagnation.seen_info:
            session.stagnation.seen_info.add(sig)
            turn_state.made_progress = True


def execute_edit_call(session: AgentLoopSession, call: ToolCall) -> ToolOutcome:
    path = call_arg(call, "path", ".")
    if edit_has_content(call):
        canonical = canonical_project_path(session.project, path)
        if safe_join(session.project, canonical).is_file():
            outcome = ToolOutcome.error(
                "content is only allowed when creating a new file; "
                f"use replacements for existing file: {canonical}"
            )
        else:
            if session.change_tracker is not None:
                session.change_tracker.capture_before(path)
            outcome = session.tool_fns.write_file(
                session.project,
                path,
                call_arg(call, "content"),
            )
    else:
        guard = read_before_edit_outcome(
            session.project,
            path,
            session.progress.known_file_paths,
        )
        if guard is not None:
            outcome = guard
        else:
            if session.change_tracker is not None:
                session.change_tracker.capture_before(path)
            outcome = session.tool_fns.edit_file(
                session.project,
                path,
                edit_blocks_from_call(call),
            )
    if outcome.ok and outcome.changed:
        if session.change_tracker is not None:
            session.change_tracker.capture_after(path)
        record_edit_change(
            session,
            canonical_project_path(session.project, path),
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
    outcome = session.tool_fns.execute_run_command(
        session.project,
        path,
        command,
        permission_profile=session.profile.name,
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
        return tool_fns.search_files(
            project,
            path,
            call_arg(call, "query"),
        )
    if call.name == "references":
        return tool_fns.find_references(
            project,
            path,
            call_arg(call, "symbol"),
        )
    return ToolOutcome.error(f"unsupported information tool {call.name} (path={path})")


def execute_tool_call(
    session: AgentLoopSession,
    call: ToolCall,
    *,
    turn: int,
    tool_index: int,
) -> ToolOutcome:
    if call.name == "edit":
        return execute_edit_call(session, call)
    if call.name in ("read", "ls", "search", "references"):
        return execute_information_tool_call(session.project, session.tool_fns, call)
    if call.name == "run":
        return execute_run_call(session, call, turn=turn, tool_index=tool_index)
    path = call_arg(call, "path", ".")
    return ToolOutcome.error(f"malformed tool call {call.name} (path={path})")


__all__ = [
    "INFORMATION_TOOL_NAMES",
    "SUPPORTED_TOOL_NAMES",
    "TurnState",
    "call_arg",
    "emit_tool_started_after_intent",
    "evaluate_tool_call_policy",
    "evaluate_tool_call_policy_for",
    "execute_edit_call",
    "execute_information_tool_call",
    "execute_run_call",
    "execute_tool_call",
    "mark_policy_denied_run",
    "policy_asks_user",
    "policy_denied",
    "policy_error_outcome",
    "record_tool_call_intent",
    "record_tool_call_settlement",
    "record_tool_outcome",
    "request_shell_approval",
    "tool_error_outcome",
    "tool_result_from_outcome",
]
