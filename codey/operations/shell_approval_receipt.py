"""Read the existing approval command and result; never execute a command."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def resolve_shell_approval(durable: Any, outputs: Any, approval_id: str) -> dict[str, Any] | None:
    record = durable.approval(approval_id)
    if str(record.status) not in {"execution_settled", "denied"} or not isinstance(record.settlement, Mapping):
        return None
    result_ref = record.settlement.get("result_ref")
    result_digest = record.settlement.get("result_digest")
    if not result_ref or not result_digest:
        return None
    command = read_approval_command(outputs, record)
    result = outputs.read_json_artifact({"session_id": record.conversation_id, "run_id": record.task_id,
        "handle": result_ref, "sha256": result_digest, "schema_version": 1})
    if not isinstance(result, dict):
        raise ValueError("approval result is not an object")
    return {"call_id": str(command.get("call_id") or ""),
        "tool_name": str(command.get("tool_name") or "shell"), "tool_args": command.get("tool_args"),
        "command": str(command.get("command") or ""), "cwd": str(command.get("cwd") or "."),
        "model_text": str(result.get("output") or result.get("error") or ""),
        "ok": result.get("ok") is True and str(record.status) == "execution_settled",
        "exit_code": result.get("exit_code") if type(result.get("exit_code")) is int else None,
        "error_code": str(result.get("status") or result.get("error") or ""),
        "truncated": bool(result.get("truncated")), "window_ref": command.get("window_ref"),
        "approval_id": approval_id, "source_run_id": record.task_id}


def read_approval_command(outputs: Any, record: Any) -> dict[str, Any]:
    command = outputs.read_json_artifact({"session_id": record.conversation_id, "run_id": record.task_id,
        "handle": record.command_ref, "sha256": record.command_digest, "schema_version": 1})
    if not isinstance(command, dict):
        raise ValueError("approval command is not an object")
    return command
