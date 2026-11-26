"""Admission of non-allowlisted commands into the existing approval workflow."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from codey.policies.action import DECISION_ASK_USER, DECISION_DENY, ActionSubject, evaluate_action
from codey.policies.command_line import ShellSpec
from codey.policies.shell_analysis import ShellAnalysis, analyze_shell, prepare_shell_command

if TYPE_CHECKING:
    from codey.runtime.core.models import ToolCall


def prepare_approval_call(call: ToolCall, *, project: Path, permission_profile: str,
                          shell: ShellSpec) -> tuple[ToolCall, ShellAnalysis] | None:
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
    raw_command = call.args.get("command")
    if not isinstance(raw_command, str):
        return None
    command = raw_command.strip()
    if call.name == "shell" and shell.kind == "powershell":
        command, analysis = prepare_shell_command(command, shell)
        # Approval records strip outer whitespace; bind their exact saved text.
        if command != command.strip():
            command = command.strip()
            analysis = analyze_shell(command, shell)
    else:
        analysis = analyze_shell(command, shell)
    call = replace(call, args={**call.args, "command": command})
    decision = evaluate_action(ActionSubject(
        kind="shell", project=str(project), permission_profile=permission_profile,
        path=str(call.args.get("path") or "."), command=command,
        approval_available=True,
        shell=shell,
        analysis=analysis,
    ))
    return (call, analysis) if decision.decision == DECISION_ASK_USER else None
