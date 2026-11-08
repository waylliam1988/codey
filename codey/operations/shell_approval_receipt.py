"""Read the existing approval command and result; never execute a command."""
from __future__ import annotations

from typing import Protocol, TypedDict

from codey.durable.model import ApprovalRecord
from codey.runtime.core.execution_receipt import ExecutionReceipt
from codey.storage.managed_outputs import ManagedOutputStore
from codey.types.json_value import (
    JsonObject,
    decode_json_object,
    require_json_boolean,
    require_json_integer,
    require_json_string,
)


class ApprovalReader(Protocol):
    def approval(self, approval_id: str) -> ApprovalRecord: ...


class ApprovalCommand(TypedDict):
    call_id: str
    tool_name: str
    tool_args: JsonObject
    command: str
    cwd: str
    turn: int
    tool_index: int
    window_ref: JsonObject | None


class ShellApprovalOutcome(TypedDict):
    call_id: str
    tool_name: str
    tool_args: JsonObject
    command: str
    cwd: str
    model_text: str
    ok: bool
    exit_code: int | None
    error_code: str
    truncated: bool
    capture_truncated: bool
    execution_id: str
    execution: JsonObject
    window_ref: JsonObject | None
    approval_id: str
    source_run_id: str


def resolve_shell_approval(durable: ApprovalReader, outputs: ManagedOutputStore,
                           approval_id: str) -> ShellApprovalOutcome | None:
    record = durable.approval(approval_id)
    if str(record.status) not in {"execution_settled", "denied"} or record.settlement is None:
        return None
    result_ref = record.settlement.get("result_ref")
    result_digest = record.settlement.get("result_digest")
    if not result_ref or not result_digest:
        return None
    command = read_approval_command(outputs, record)
    result = decode_json_object(outputs.read_json_artifact({"session_id": record.conversation_id, "run_id": record.task_id,
        "handle": result_ref, "sha256": result_digest, "schema_version": 1}))
    ok = require_json_boolean(result.get("ok"), "ok") and str(record.status) == "execution_settled"
    receipt = ExecutionReceipt.from_payload(result).bounded()
    return {"call_id": command["call_id"], "tool_name": command["tool_name"], "tool_args": command["tool_args"],
            "command": command["command"], "cwd": command["cwd"], "model_text": receipt.render(),
            "ok": ok, "exit_code": receipt.exit_code, "error_code": receipt.status,
            "truncated": receipt.truncated, "capture_truncated": receipt.capture_truncated,
            "execution_id": receipt.execution_id, "execution": receipt.facts(), "window_ref": command["window_ref"],
            "approval_id": approval_id, "source_run_id": record.task_id}


def read_approval_command(outputs: ManagedOutputStore, record: ApprovalRecord) -> ApprovalCommand:
    command = decode_json_object(outputs.read_json_artifact({"session_id": record.conversation_id, "run_id": record.task_id,
        "handle": record.command_ref, "sha256": record.command_digest, "schema_version": 1}))
    window = command.get("window_ref")
    return {"call_id": require_json_string(command.get("call_id", ""), "call_id"),
            "tool_name": require_json_string(command.get("tool_name", "shell"), "tool_name"),
            "tool_args": decode_json_object(command.get("tool_args", {})),
            "command": require_json_string(command.get("command", ""), "command"),
            "cwd": require_json_string(command.get("cwd", "."), "cwd"),
            "turn": require_json_integer(command.get("turn", 0), "turn"),
            "tool_index": require_json_integer(command.get("tool_index", 0), "tool_index"),
            "window_ref": decode_json_object(window) if window is not None else None}
