"""Admission of non-allowlisted commands into the existing approval workflow."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from codey.policies.action import DECISION_ASK_USER, DECISION_DENY, ActionSubject, evaluate_action
from codey.policies.command_line import ShellSpec, resolve_shell

if TYPE_CHECKING:
    from codey.runtime.core.models import ToolCall


def shell_compatibility_error(command: str, *, shell: ShellSpec | None = None) -> str:
    """Do not ask Windows users to approve syntax the executor cannot run."""
    return (shell if shell is not None else resolve_shell()).compatibility_error(command)


def approval_call(call: ToolCall, *, project: Path, permission_profile: str,
                  shell: ShellSpec | None = None) -> ToolCall | None:
    """Only allowlist refusals may ask for shell access; other denials remain final."""
    if call.name not in {"run", "shell"}:
        return None
    if call.name == "run":
        from codey.toolchain.command_output import tail_lines

        try:
            tail_lines(call.args.get("output_tail_lines"))
        except ValueError:
            return None
        decision = evaluate_action(ActionSubject(
            kind="run_command", project=str(project), permission_profile=permission_profile,
            path=str(call.args.get("path") or "."), command=str(call.args.get("command") or ""),
            shell=shell,
        ))
        if decision.decision != DECISION_DENY or decision.reason_code != "command_not_allowed":
            return None
    candidate = replace(call, name="shell")
    decision = evaluate_action(ActionSubject(
        kind="shell", project=str(project), permission_profile=permission_profile,
        path=str(candidate.args.get("path") or "."), command=str(candidate.args.get("command") or ""),
        approval_available=True,
        shell=shell,
    ))
    return call if decision.decision == DECISION_ASK_USER else None
