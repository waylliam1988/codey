"""Tool policy, dispatch, and result accounting for the agent loop."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from codey.agents.protocol import (
    canonical_project_path,
    edit_has_content,
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
        return cast(ToolOutcome, tool_fns.read_file(project, path, **read_options))
    if call.name == "ls":
        return cast(ToolOutcome, tool_fns.list_directory(project, path))
    if call.name == "search":
        search_options = {
            name: call.args[name]
            for name in ("offset", "limit")
            if name in call.args
        }
        return cast(ToolOutcome, tool_fns.search_files(
            project,
            path,
            call_arg(call, "query"),
            **search_options,
        ))
    if call.name == "references":
        return cast(ToolOutcome, tool_fns.find_references(
            project,
            path,
            call_arg(call, "symbol"),
        ))
    return ToolOutcome.error(f"unsupported information tool {call.name} (path={path})")


__all__ = [
    "INFORMATION_TOOL_NAMES",
    "SUPPORTED_TOOL_NAMES",
    "action_subject_for_call",
    "call_arg",
    "evaluate_tool_call_policy_for",
    "execute_information_tool_call",
    "policy_asks_user",
    "policy_denied",
    "read_before_edit_outcome",
]
