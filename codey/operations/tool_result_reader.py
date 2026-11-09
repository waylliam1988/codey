"""Read verified task-owned receipts; never turn missing output into execution."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Protocol

from codey.operations.task_session import TaskSession
from codey.runtime.core.execution_receipt import ExecutionOutputReference, execution_receipt
from codey.runtime.core.models import ToolCall, ToolResult
from codey.storage.execution_output_store import ExecutionOutputStore
from codey.storage.managed_outputs import ManagedOutputStore
from codey.types.json_value import JsonObject, JsonValue, require_json_string


class ReceiptReaderContext(Protocol):
    session: TaskSession
    managed_outputs: ManagedOutputStore | None
    session_id: str
    run_id: str


@dataclass(frozen=True)
class ResolvedOutput:
    text: str
    source: str
    reference: JsonObject
    display_truncated: bool
    capture_truncated: bool
    storage_truncated: bool
    storage_complete: bool | None
    original_output_incomplete: bool
    full_output_recoverable: bool


def resolve_result_output(context: ReceiptReaderContext, result: ToolResult) -> ResolvedOutput:
    """Choose one authoritative output; never recover by executing a tool."""
    execution = execution_receipt(result)
    capture = execution.capture_truncated if execution is not None else result.audit.get("capture_truncated") is True
    ref = execution.output_ref if execution is not None else None
    # Historical receipts bind retained output with these explicit flags.
    # A diagnostic execution ID on its own is not an external output binding.
    legacy = (execution is not None and bool(execution.execution_id) and result.truncated
              and (execution.full_output_retained or execution.capture_truncated))
    if ref is not None or legacy:
        if context.managed_outputs is None:
            raise ValueError("execution output store unavailable")
        assert execution is not None
        identity = ref.execution_id if ref is not None else execution.execution_id
        if execution.execution_id != identity:
            raise ValueError("execution output reference differs from receipt identity")
        if ref is not None and (ref.session_id != context.session_id or ref.run_id != context.run_id):
            raise ValueError("execution output belongs to another task")
        snapshot = ExecutionOutputStore.read_existing(context.managed_outputs.root.parent, identity)
        if snapshot["session_id"] != context.session_id or snapshot["run_id"] != context.run_id:
            raise ValueError("execution output belongs to another task")
        actual = ExecutionOutputReference.from_snapshot(snapshot)
        if ref is not None and actual != ref:
            raise ValueError("execution output digest or revision differs from receipt")
        if execution.exit_code is not None and snapshot["exit_code"] != execution.exit_code:
            raise ValueError("execution output exit code differs from receipt")
        text = snapshot["stdout"]["text"]
        if snapshot["stderr"]["text"]:
            text += "\n[stderr]\n" + snapshot["stderr"]["text"]
        stored_truncated = snapshot["stdout"]["truncated"] or snapshot["stderr"]["truncated"]
        complete = not stored_truncated and snapshot["execution_state"] == "settled"
        incomplete = capture or not complete
        return ResolvedOutput(text, "execution", actual.facts(), result.truncated, capture,
                              stored_truncated, complete, incomplete, not incomplete)
    managed = result.audit.get("managed_output")
    if managed is not None:
        if not isinstance(managed, dict) or context.managed_outputs is None:
            raise ValueError("stored output unavailable")
        handle = require_json_string(managed.get("handle"), "managed output handle")
        digest = require_json_string(managed.get("sha256"), "managed output digest")
        text, metadata = context.managed_outputs.read_tool_output(context.session_id, context.run_id, handle)
        if metadata.get("sha256") != digest:
            raise ValueError("stored output digest differs from receipt")
        stored_truncated = metadata.get("stored_truncated") is True
        incomplete = stored_truncated or capture
        return ResolvedOutput(text, "managed", {"handle": handle, "sha256": digest}, result.truncated,
                              capture, stored_truncated, not stored_truncated, incomplete, not incomplete)
    incomplete = result.truncated or capture
    recoverable = not incomplete and (execution.full_output_recoverable if execution is not None
                                      else result.call.name not in {"shell", "run"})
    return ResolvedOutput(result.model_text, "receipt", {}, result.truncated, capture, False,
                          None, incomplete, recoverable)


def read_tool_result(delegate: ReceiptReaderContext, call: ToolCall) -> ToolResult:
    known: JsonObject = {}
    try:
        if not delegate.session.policy.allows("control"):
            raise ValueError("receipt access denied")
        identity = call.args.get("result_ref")
        if not isinstance(identity, str):
            raise ValueError("stored result unavailable in this task")
        if identity.startswith("rc"):
            from codey.operations.reference_handles import ReferenceHandleError, ReferenceHandleResolver

            try:
                identity = ReferenceHandleResolver(delegate.session).resolve(identity).ref
            except ReferenceHandleError as exc:
                raise ValueError(str(exc)) from exc
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
            raise ValueError("stored result unavailable in this task; use a receipt handle or complete result_ref, "
                             "not an out_ artifact handle, search result id or source id")
        execution = execution_receipt(result)
        if execution is not None:
            known["execution"] = execution.facts()
        available_views: list[JsonValue] = ["output", "value"] if "value" in result.canonical else ["output"]
        if view == "value":
            if "value" not in result.canonical:
                raise ValueError("canonical value unavailable in this receipt; available views: output")
            text = json.dumps({"value": result.canonical["value"]}, ensure_ascii=False,
                              sort_keys=True, separators=(",", ":"), allow_nan=False)
            resolved = ResolvedOutput(text, "value", {}, False, False, False, None, False, True)
        else:
            resolved = resolve_result_output(delegate, result)
            text = resolved.text
        match_offset = None
        if isinstance(query, str):
            found = text.find(query, offset)
            if found >= 0:
                match_offset = found
                offset = max(offset, found - min(200, limit // 4))
        excerpt = text[offset:offset + limit] if query is None or match_offset is not None else ""
        exit_code = execution.exit_code if execution is not None else result.audit.get("exit_code")
        if exit_code is not None and type(exit_code) is not int:
            raise ValueError("receipt has an invalid exit code")
        data: JsonObject = {"result_ref": identity, "view": view, "ok": result.ok, "exit_code": exit_code,
                "offset": offset, "next_offset": offset + limit if offset + limit < len(text) else None,
                "stored_chars": len(text), "original_output_incomplete": resolved.original_output_incomplete,
                "text": excerpt, "available_views": available_views, "capture_truncated": resolved.capture_truncated,
                "storage_truncated": resolved.storage_truncated, "display_truncated": resolved.display_truncated,
                "page_truncated": offset > 0 or len(excerpt) < len(text),
                "storage_complete": resolved.storage_complete, "output_source": resolved.source,
                "output_reference": resolved.reference, "full_output_recoverable": resolved.full_output_recoverable}
        if execution is not None:
            data["execution"] = execution.facts()
        if query is not None:
            data["match_offset"] = match_offset
            if match_offset is None:
                data["next_offset"] = None
        return ToolResult(call, json.dumps(data, ensure_ascii=False), ok=True,
                          canonical={"receipt_page": {"result_ref": identity, "view": view,
                                                      "next_offset": data["next_offset"]}})
    except (OSError, sqlite3.Error, ValueError, KeyError) as exc:
        text = f"ERROR: {exc}. Read failure does not authorize re-execution."
        if known:
            text += "\nKnown execution facts: " + json.dumps(known["execution"], ensure_ascii=False)
        return ToolResult(call, text, ok=False, canonical={**known, "output_available": False})
