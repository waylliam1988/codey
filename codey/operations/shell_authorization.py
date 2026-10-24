"""Read shell denials from the owning durable task before admission/execution."""
from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from codey.durable.model import ApprovalRecord, ApprovalStatus
from codey.policies.command_line import ShellSpec
from codey.policies.shell_analysis import request_analysis
from codey.runtime.core.models import ToolCall


def denied_shell_message(call: ToolCall, *, durable: Any, managed_outputs: Any,
                         run_id: str, project: Any, shell: ShellSpec) -> str:
    """No model-generated approval ID can supersede a human denial.

    Scope is the original task and exact command/cwd/interpreter. A fresh
    human task is admitted separately by the existing input boundary. There
    is no in-memory blacklist, secondary decision log or restart exception.
    """
    if call.name not in {"run", "shell"} or durable is None:
        return ""
    try:
        task = durable.task(run_id)
        approvals = task.state.get("approvals", {})
        if not isinstance(approvals, Mapping):
            raise ValueError("invalid approval state")
        for raw in approvals.values():
            if not isinstance(raw, Mapping):
                raise ValueError("invalid approval record")
            fact = ApprovalRecord.from_payload(raw)
            if fact.status != ApprovalStatus.DENIED:
                continue
            if fact.task_id != task.task_id or fact.conversation_id != task.conversation_id:
                raise ValueError("approval owner mismatch")
            if managed_outputs is None:
                raise ValueError("approval artifact store unavailable")
            payload = managed_outputs.read_json_artifact({
                "session_id": task.conversation_id, "run_id": task.task_id,
                "handle": fact.command_ref, "sha256": fact.command_digest, "schema_version": 1,
            })
            if not isinstance(payload, dict):
                raise ValueError("invalid approval artifact")
            saved_shell, _ = request_analysis(payload)
            saved_project = str(payload["project"])
            actual_project = str(project) if project is not None else saved_project
            saved_cwd = os.path.normcase(str(Path(saved_project, str(payload["cwd"])).resolve()))
            actual_cwd = os.path.normcase(str(Path(actual_project, str(call.args.get("path") or ".")).resolve()))
            if (saved_shell == shell and saved_cwd == actual_cwd
                    and str(payload["command"]).strip() == str(call.args.get("command") or "").strip()):
                return ("This command was denied by the user in this task; it was not executed. "
                        "Do not request it again. A new user request is required to reconsider it. "
                        "Continue with other authorized work or report the limitation.")
    except (KeyError, OSError, TypeError, ValueError) as exc:
        return f"Cannot verify prior shell authorization ({type(exc).__name__}); command was not executed."
    return ""
