"""Read verified task-owned receipts; never turn missing output into execution."""
from __future__ import annotations

import json
from typing import Any

from codey.runtime.core.models import ToolCall, ToolResult


def read_tool_result(delegate: Any, call: ToolCall) -> ToolResult:
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
        result = delegate.session._memory_results.get(identity)
        if result is None:
            # Behavioral probes run outside the model tool loop. Their real
            # archive is addressed by its own scoped, digest-verified handle;
            # never fabricate a kernel execution or accept arbitrary paths.
            if delegate.managed_outputs is None:
                raise ValueError("stored result unavailable in this task")
            text, metadata = delegate.managed_outputs.read_tool_output(delegate.session_id, delegate.run_id, identity)
            if metadata.get('tool_name') != 'behavioral_verification':
                raise ValueError("stored result unavailable in this task")
            observed = json.loads(text)
            if not isinstance(observed, dict) or observed.get('status') not in {'pass', 'fail', 'not_run'}:
                raise ValueError('invalid behavioral result')
            result = ToolResult(call, text, ok=observed['status'] == 'pass',
                                truncated=metadata.get('stored_truncated') is True)
        text = result.model_text
        managed = result.audit.get("managed_output")
        clipped = result.truncated
        if isinstance(managed, dict):
            if delegate.managed_outputs is None:
                raise ValueError("stored output unavailable")
            text, metadata = delegate.managed_outputs.read_tool_output(delegate.session_id, delegate.run_id, managed["handle"])
            if metadata.get("sha256") != managed.get("sha256"):
                raise ValueError("stored output digest differs from receipt")
            clipped = metadata.get("stored_truncated") is True or result.audit.get("capture_truncated") is True
        match_offset = None
        if isinstance(query, str):
            found = text.find(query, offset)
            if found >= 0:
                match_offset = found
                offset = max(offset, found - min(200, limit // 4))
        excerpt = text[offset:offset + limit] if query is None or match_offset is not None else ""
        data = {"result_ref": identity, "ok": result.ok, "exit_code": result.audit.get("exit_code"),
                "offset": offset, "next_offset": offset + limit if offset + limit < len(text) else None,
                "stored_chars": len(text), "original_output_incomplete": clipped, "text": excerpt}
        if query is not None:
            data["match_offset"] = match_offset
            if match_offset is None:
                data["next_offset"] = None
        return ToolResult(call, json.dumps(data, ensure_ascii=False), ok=True)
    except (OSError, ValueError, KeyError) as exc:
        return ToolResult(call, f"ERROR: {exc}. Read failure does not authorize re-execution.", ok=False)
