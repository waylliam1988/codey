"""Admission of non-allowlisted commands into the existing approval workflow."""

from __future__ import annotations

import re
import sys
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from codey.policies.action import DECISION_ASK_USER, DECISION_DENY, ActionSubject, evaluate_action

if TYPE_CHECKING:
    from codey.runtime.core.models import ToolCall


def shell_compatibility_error(command: str) -> str:
    """Do not ask Windows users to approve syntax the executor cannot run."""
    if sys.platform == "win32" and _has_unquoted_heredoc(command):
        if not re.match(r"^\s*(?:\"[^\"]*bash(?:\.exe)?\"|\S*bash(?:\.exe)?)\s+-", command):
            return "Bash heredoc is not supported by the Windows command shell; use an explicit Bash command or a Windows-compatible command."
    return ""


def _has_unquoted_heredoc(command: str) -> bool:
    quoted = False
    escaped = False
    for index, char in enumerate(command):
        if escaped:
            escaped = False
        elif char == "^":
            escaped = True
        elif char == '"':
            quoted = not quoted
        elif not quoted and command[index:index + 2] == "<<":
            return True
    return False


def approval_call(call: ToolCall, *, project: Path, permission_profile: str) -> ToolCall | None:
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
        ))
        if decision.decision != DECISION_DENY or decision.reason_code != "command_not_allowed":
            return None
    candidate = replace(call, name="shell")
    decision = evaluate_action(ActionSubject(
        kind="shell", project=str(project), permission_profile=permission_profile,
        path=str(candidate.args.get("path") or "."), command=str(candidate.args.get("command") or ""),
        approval_available=True,
    ))
    return call if decision.decision == DECISION_ASK_USER else None
