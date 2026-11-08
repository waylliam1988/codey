"""Read verified task-owned receipts; never turn missing output into execution."""
from __future__ import annotations

import json
import sqlite3
from typing import Protocol

from codey.operations.execution_claims import execution_receipt
from codey.operations.task_session import TaskSession
from codey.runtime.core.models import ToolCall, ToolResult
from codey.storage.execution_output_store import ExecutionOutputStore
from codey.storage.managed_outputs import ManagedOutputStore
from codey.types.json_value import JsonObject, JsonValue


class ReceiptReaderContext(Protocol):
    session: TaskSession
    managed_outputs: ManagedOutputStore | None
    session_id: str
    run_id: str


def _execution_output(delegate: ReceiptReaderContext, result: ToolResult) -> tuple[str, bool, bool] | None:
    identity = result.audit.get("execution_id")
    if (result.call.name not in {"shell", "run"} or not result.truncated
            or not isinstance(identity, str) or not identity or delegate.managed_outputs is None):
        return None
    snapshot = ExecutionOutputStore.read_existing(delegate.managed_outputs.root.parent, identity)
    if snapshot["session_id"] != delegate.session_id or snapshot["run_id"] != delegate.run_id:
        raise ValueError("execution output belongs to another task")
    text = snapshot["stdout"]["text"]
    if snapshot["stderr"]["text"]:
        text += "\n[stderr]\n" + snapshot["stderr"]["text"]
    storage_truncated = snapshot["stdout"]["truncated"] or snapshot["stderr"]["truncated"]
    incomplete = (result.audit.get("capture_truncated") is True or storage_truncated
                  or snapshot["execution_state"] != "settled")
    return text, storage_truncated, incomplete


def read_tool_result(delegate: ReceiptReaderContext, call: ToolCall) -> ToolResult:
    try:
        if not delegate.session.policy.allows("control"):
            raise ValueError("receipt access denied")
        identity = call.args.get("result_ref")
        if not isinstance(identity, str):
            raise ValueError("stored result unavailable in this task")
        offset, limit = call.args.get("offset", 0), call.args.get("limit", 4000)
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 8000:
            raise ValueError("invalid stored result page")
        query = call.args.get("query")
        if "query" in call.args and (not isinstance(query, str) or not 1 <= len(query) <= 512):
            raise ValueError("query must be a nonempty literal string of at most 512 characters")
        view = call.args.get("view", "output")
        if view not in ("output", "value"):
            raise ValueError("view must be output or value")
        result = delegate.session._memory_results.get(identity)
        if result is None:
            raise ValueError("stored result unavailable in this task; copy the complete result_ref from Result metadata, "
                             "not an out_ artifact handle, search result id or source id")
        text = result.model_text
        managed = result.audit.get("managed_output")
        clipped = result.truncated
        capture_truncated = result.audit.get("capture_truncated") is True
        storage_truncated = False
        available_views: list[JsonValue] = ["output", "value"] if "value" in result.canonical else ["output"]
        if view == "value":
            if "value" not in result.canonical:
                raise ValueError("canonical value unavailable in this receipt; available views: output")
            text = json.dumps({"value": result.canonical["value"]}, ensure_ascii=False,
                              sort_keys=True, separators=(",", ":"), allow_nan=False)
            clipped = False
        elif isinstance(managed, dict):
            if delegate.managed_outputs is None:
                raise ValueError("stored output unavailable")
            text, metadata = delegate.managed_outputs.read_tool_output(delegate.session_id, delegate.run_id, managed["handle"])
            if metadata.get("sha256") != managed.get("sha256"):
                raise ValueError("stored output digest differs from receipt")
            storage_truncated = metadata.get("stored_truncated") is True
            clipped = storage_truncated or capture_truncated
        execution_output = _execution_output(delegate, result) if view == "output" else None
        if execution_output is not None:
            text, storage_truncated, clipped = execution_output
        match_offset = None
        if isinstance(query, str):
            found = text.find(query, offset)
            if found >= 0:
                match_offset = found
                offset = max(offset, found - min(200, limit // 4))
        excerpt = text[offset:offset + limit] if query is None or match_offset is not None else ""
        exit_code = result.audit.get("exit_code")
        if exit_code is not None and type(exit_code) is not int:
            raise ValueError("receipt has an invalid exit code")
        execution = execution_receipt(result)
        recoverable = (not clipped and (execution.status == "exit" if execution is not None
                                       else result.call.name not in {"shell", "run"}))
        data: JsonObject = {"result_ref": identity, "view": view, "ok": result.ok, "exit_code": exit_code,
                "offset": offset, "next_offset": offset + limit if offset + limit < len(text) else None,
                "stored_chars": len(text), "original_output_incomplete": clipped, "text": excerpt,
                "available_views": available_views, "capture_truncated": capture_truncated,
                "storage_truncated": storage_truncated, "display_truncated": result.truncated,
                "full_output_recoverable": recoverable}
        if query is not None:
            data["match_offset"] = match_offset
            if match_offset is None:
                data["next_offset"] = None
        return ToolResult(call, json.dumps(data, ensure_ascii=False), ok=True,
                          canonical={"receipt_page": {"result_ref": identity, "view": view,
                                                      "next_offset": data["next_offset"]}})
    except (OSError, sqlite3.Error, ValueError, KeyError) as exc:
        return ToolResult(call, f"ERROR: {exc}. Read failure does not authorize re-execution.", ok=False)
