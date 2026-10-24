"""Admission of non-allowlisted commands into the existing approval workflow."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from codey.policies.action import DECISION_ASK_USER, DECISION_DENY, ActionSubject, evaluate_action
from codey.policies.command_line import ShellSpec
from codey.policies.shell_analysis import ShellAnalysis, analyze_shell

if TYPE_CHECKING:
    from codey.runtime.core.models import ToolCall


def analyze_approval_call(call: ToolCall, *, project: Path, permission_profile: str,
                           shell: ShellSpec) -> ShellAnalysis | None:
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
    command = str(call.args.get("command") or "").strip()
    analysis = analyze_shell(command, shell)
    decision = evaluate_action(ActionSubject(
        kind="shell", project=str(project), permission_profile=permission_profile,
        path=str(call.args.get("path") or "."), command=command,
        approval_available=True,
        shell=shell,
        analysis=analysis,
    ))
    return analysis if decision.decision == DECISION_ASK_USER else None
