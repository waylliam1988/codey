from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import Protocol

from codey.codemode.identity import rpc_child_index
from codey.codemode.json_edit import JSON_FIELD_EDIT_GUIDANCE, replace_json_fields
from codey.codemode.types import ToolCatalogEntry
from codey.runtime.core.models import ToolCall, ToolResult
from codey.toolchain.constants import STALE_READ_REFERENCE_CODE, STALE_READ_REFERENCE_ERROR
from codey.toolchain.result_schema import validate_output
from codey.types.json_value import JsonObject, decode_json_object


class CodeModeHostProtocol(Protocol):
    def invoke(self, name: str, args: dict[str, object]) -> object:
        """Invoke one frozen, host-mediated child tool."""


def _normalize_open_result_args(args: JsonObject) -> JsonObject:
    if set(args) != {"id"}:
        return args
    result_id = args["id"]
    if not isinstance(result_id, str) or re.fullmatch(r"r[1-9][0-9]*", result_id) is None:
        raise ValueError("open_result requires an observed search reference such as r2")
    return {"result_id": result_id}


def successful_tool_value(raw: ToolResult, entry: ToolCatalogEntry) -> object:
    schema = entry.output_schema
    if schema.get("type") == "string":
        value: object = raw.model_text
    else:
        if "value" not in raw.canonical:
            raise ValueError(f"missing structured result for {entry.name}")
        value = raw.canonical["value"]
    validate_output(entry.name, schema, value)
    return value


class CodeModeHost:
    """Translate sandbox RPC calls into the existing kernel tool callback."""

    def __init__(
        self,
        *,
        execution_id: str,
        catalog: Sequence[ToolCatalogEntry],
        execute: Callable[[ToolCall], ToolResult],
        allow_writes: bool = False,
        replay: Callable[[int, str, JsonObject], ToolResult] | None = None,
    ) -> None:
        self.execution_id = str(execution_id or "").strip()
        if not self.execution_id:
            raise ValueError("execution_id must be non-empty")
        if any(not entry.read_only for entry in catalog) and not allow_writes:
            raise ValueError("CodeMode read-only host requires allow_writes=True for write tools")
        if any(not entry.read_only and not (
            entry.grant == "project.write" or entry.namespace == "browser"
            and entry.grant in {"browser.interact", "browser.effect"}
        ) for entry in catalog):
            raise ValueError("CodeMode only permits project.write tools or authorized browser actions")
        self._allow_writes = bool(allow_writes)
        self._catalog = {entry.name: entry for entry in catalog}
        self._execute = execute
        self._replay = replay
        self._next_child = 0
        self._complete_reads: dict[str, tuple[str, str]] = {}

    def invoke(self, name: str, args: dict[str, object]) -> object:
        tool_name = str(name or "").strip().lower()
        if tool_name == "codemode" or tool_name.startswith("codemode."):
            raise ValueError("recursive codemode invocation is denied")
        if tool_name not in self._catalog:
            raise ValueError(f"tool is not in the frozen catalog: {tool_name}")
        if not isinstance(args, dict):
            raise ValueError("tool args must be an object")
        index = rpc_child_index.get()
        if index is None:
            index = self._next_child
            self._next_child += 1
        decoded_args = decode_json_object(args)
        if tool_name == "open_result":
            decoded_args = _normalize_open_result_args(decoded_args)
        json_edit = tool_name == "edit" and "changes" in decoded_args
        if json_edit:
            try:
                decoded_args = self._json_edit_args(decoded_args)
            except (ValueError, TypeError, RecursionError) as exc:
                return {"ok": False, "error": f"JSON field edit rejected: {exc}"}
        if self._replay is not None:
            raw = self._replay(index, tool_name, decoded_args)
        else:
            call = ToolCall(tool_name, decoded_args, call_id=f"{self.execution_id}:child:{index}")
            raw = self._execute(call)
        if not isinstance(raw, ToolResult):
            raise TypeError("host executor must return a ToolResult")
        if not raw.ok:
            error = raw.model_text
            if json_edit and raw.audit.get("error_code") == STALE_READ_REFERENCE_CODE:
                error = f"{STALE_READ_REFERENCE_ERROR} {JSON_FIELD_EDIT_GUIDANCE}"
            return {"ok": False, "error": error}
        value = successful_tool_value(raw, self._catalog[tool_name])
        output: dict[str, object] = {"ok": True, "value": value}
        if ref := raw.audit.get("result_ref"):
            output["result_ref"] = ref
            if (tool_name == "read_file" and "edit" in self._catalog
                    and isinstance(ref, str) and isinstance(value, dict) and not raw.truncated):
                data = decode_json_object(value)
                path, text = decoded_args.get("path"), data.get("text")
                if (isinstance(path, str) and isinstance(text, str) and data.get("text_complete") is True
                        and data.get("start_line") == 1 and data.get("next_offset") == 0
                        and data.get("end_line") == data.get("total_lines")):
                    self._complete_reads[ref] = (path, text)
        return output

    def _json_edit_args(self, args: JsonObject) -> JsonObject:
        ref = args.get("base_result_ref")
        if set(args) != {"base_result_ref", "changes"} or not isinstance(ref, str) or ref not in self._complete_reads:
            raise ValueError("use an original complete tools.read_file result from this program")
        path, text = self._complete_reads[ref]
        content = replace_json_fields(text, decode_json_object(args["changes"]))
        return {"path": path, "content": content, "base_result_ref": ref}
