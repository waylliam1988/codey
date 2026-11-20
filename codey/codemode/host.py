from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import Protocol

from codey.codemode.identity import rpc_child_admission, rpc_child_allocator, rpc_child_index
from codey.codemode.json_edit import JSON_FIELD_EDIT_GUIDANCE, replace_json_fields
from codey.codemode.types import ToolCatalogEntry
from codey.operations.browser_references import BrowserReferences
from codey.runtime.core.models import ToolCall, ToolResult
from codey.toolchain.constants import STALE_READ_REFERENCE_CODE, STALE_READ_REFERENCE_ERROR
from codey.toolchain.result_schema import validate_output
from codey.toolchain.tool_spec import _check_spec_value_against_schema
from codey.types.json_value import JsonObject, JsonValue, canonical_json_object, decode_json_object


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
    from codey.toolchain.result_schema import completed_output_schema

    schema = completed_output_schema(entry.output_schema)
    if raw.kind == "accepted":
        value: object = raw.canonical.get("value")
    elif schema.get("type") == "string":
        value = raw.model_text
    else:
        if "value" not in raw.canonical:
            raise ValueError(f"missing structured result for {entry.name}")
        value = raw.canonical["value"]
    validate_output(entry.name, entry.output_schema, value)
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
        task_navigation_url: str | None = None,
        browser_references: BrowserReferences | None = None,
        replay_browser_ref: Callable[[int, str, JsonValue], str] | None = None,
        browser_stage: Callable[[], int] | None = None,
        original_browser_wait: Callable[[int], JsonObject | None] | None = None,
        bound_edit: Callable[[int, JsonObject], JsonObject] | None = None,
    ) -> None:
        self.execution_id = str(execution_id or "").strip()
        if not self.execution_id:
            raise ValueError("execution_id must be non-empty")
        if any(not entry.read_only for entry in catalog) and not allow_writes:
            raise ValueError("CodeMode read-only host requires allow_writes=True for write tools")
        if any(not entry.read_only and not (
            entry.grant == "project.write"
            or (entry.namespace == "project" and entry.name == "run" and entry.grant == "project.verify")
            or entry.namespace == "browser"
            and entry.grant in {"browser.interact", "browser.effect"}
            or entry.namespace == "task_control" and entry.grant == "control"
        ) for entry in catalog):
            raise ValueError("CodeMode only permits authorized project actions and browser actions")
        self._allow_writes = bool(allow_writes)
        self._catalog = {entry.name: entry for entry in catalog}
        self._execute = execute
        self._replay = replay
        self._task_navigation_url = task_navigation_url
        self._browser_references = browser_references
        self._replay_browser_ref = replay_browser_ref
        self._browser_stage = browser_stage
        self._original_browser_wait = original_browser_wait
        self._bound_edit = bound_edit
        self._next_child = 0
        self._complete_reads: dict[str, ToolResult] = {}

    def invoke(self, name: str, args: dict[str, object]) -> object:
        tool_name = str(name or "").strip().lower()
        if tool_name == "codemode" or tool_name.startswith("codemode."):
            raise ValueError("recursive codemode invocation is denied")
        if tool_name not in self._catalog:
            raise ValueError(f"tool is not in the frozen catalog: {tool_name}")
        if not isinstance(args, dict):
            raise ValueError("tool args must be an object")
        decoded_args: JsonObject = decode_json_object(args)
        index = self._current_child_index()
        json_edit = tool_name == "edit" and "changes" in decoded_args
        click_args, query, steps = self._click_args(tool_name, decoded_args)
        step_indices = self._allocate_children(len(steps))
        raw = self._invoke_one(index, tool_name, click_args, query=query)
        if raw.ok and steps:
            for step_index, (step_name, step_args) in zip(step_indices, steps, strict=True):
                try:
                    followup = self._invoke_one(step_index, step_name, step_args, query=query)
                except (ValueError, TypeError, RecursionError) as exc:
                    error = str(exc)
                else:
                    if followup.ok:
                        continue
                    error = followup.model_text
                return {"ok": False, "error": f"Click delivered; {step_name} failed: {error}"
                        ". Continue from the committed receipts; do not repeat the click."}
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
                    self._complete_reads[ref] = raw
        return output

    def _current_child_index(self) -> int:
        index = rpc_child_index.get()
        if index is not None:
            self._next_child = max(self._next_child, index + 1)
            return index
        return self._allocate_children(1)[0]

    def _allocate_children(self, count: int) -> tuple[int, ...]:
        allocator = rpc_child_allocator.get()
        if allocator is not None:
            return allocator(count)
        indices = tuple(range(self._next_child, self._next_child + count))
        self._next_child += count
        return indices

    def _invoke_one(
        self, index: int, tool_name: str, args: JsonObject, *, query: bool,
    ) -> ToolResult:
        if admit := rpc_child_admission.get():
            admit()
        if tool_name not in self._catalog:
            raise ValueError(f"tool is not in the frozen catalog: {tool_name}")
        decoded_args = self._reference_args(index, tool_name, args)
        if tool_name == "browser_wait":
            from codey.operations.browser_waits import complete_wait_args

            parameters = canonical_json_object(self._catalog[tool_name].parameters)
            properties = decode_json_object(parameters.get("properties", {}))
            decoded_args = complete_wait_args(decoded_args, properties.get("timeout_ms"),
                stage=self._browser_stage() if self._browser_stage is not None else 0,
                original=self._original_browser_wait(index) if self._original_browser_wait is not None else None)
            if error := _check_spec_value_against_schema(tool_name, "args", parameters, decoded_args, strict=True):
                raise ValueError(error)
        if tool_name == "edit" and self._bound_edit is not None:
            ref = decoded_args.get("base_result_ref")
            if not isinstance(ref, str) or ref not in self._complete_reads:
                raise ValueError("bound edit requires this program's original complete read receipt")
            decoded_args = self._bound_edit(index, decoded_args)
        if tool_name == "open_result":
            decoded_args = _normalize_open_result_args(decoded_args)
        if tool_name == "browser_navigate" and "target" in decoded_args:
            if set(decoded_args) != {"target"} or decoded_args["target"] != "task":
                raise ValueError('Use either {target: "task"} or {url: "..."}')
            if self._task_navigation_url is None:
                raise ValueError("This turn has no bound task URL")
            decoded_args = decode_json_object({"url": self._task_navigation_url})
        json_edit = tool_name == "edit" and "changes" in decoded_args
        if json_edit:
            try:
                decoded_args = self._json_edit_args(decoded_args)
            except (ValueError, TypeError, RecursionError) as exc:
                return ToolResult(ok=False, call=ToolCall(tool_name, decoded_args), model_text=f"JSON field edit rejected: {exc}")
        from codey.operations.browser_followups import query_click

        token = query_click.set(query)
        try:
            if self._replay is not None:
                raw = self._replay(index, tool_name, decoded_args)
            else:
                call = ToolCall(tool_name, decoded_args, call_id=f"{self.execution_id}:child:{index}")
                raw = self._execute(call)
        finally:
            query_click.reset(token)
        if not isinstance(raw, ToolResult):
            raise TypeError("host executor must return a ToolResult")
        return raw

    def _reference_args(self, index: int, tool_name: str, args: JsonObject) -> JsonObject:
        if tool_name not in {"browser_fill", "browser_click"} or not isinstance(selector := args.get("ref"), dict):
            return args
        if self._replay_browser_ref is not None:
            resolved_ref = self._replay_browser_ref(index, tool_name, selector)
        elif self._browser_references is not None:
            resolved_ref = self._browser_references.resolve(selector)
        else:
            raise ValueError("No complete current control binding; use an observed string ref or take a fresh snapshot.")
        return {**args, "ref": resolved_ref}

    def _click_args(
        self, name: str, args: JsonObject,
    ) -> tuple[JsonObject, bool, tuple[tuple[str, JsonObject], ...]]:
        if name != "browser_click" or "purpose" not in args:
            return args, False, ()
        plans = canonical_json_object(self._catalog[name].parameters).get("x-codey-click-plans")
        purpose = args["purpose"]
        if (not isinstance(plans, dict) or not isinstance(purpose, str) or purpose not in plans
                or set(args) != {"ref", "purpose"}):
            raise ValueError("The requested click follow-up is unavailable or its arguments are invalid.")
        steps = plans[purpose]
        if not isinstance(steps, list):
            raise ValueError("The requested click follow-up is unavailable or its arguments are invalid.")
        parsed: list[tuple[str, JsonObject]] = []
        for item in steps:
            if not isinstance(item, dict) or set(item) != {"name", "args"}:
                raise ValueError("The requested click follow-up is unavailable or its arguments are invalid.")
            step_name = item["name"]
            if not isinstance(step_name, str) or step_name not in self._catalog:
                raise ValueError("The requested click follow-up contains an unavailable tool.")
            parsed.append((step_name, decode_json_object(item["args"])))
        return {"ref": args["ref"]}, purpose == "result", tuple(parsed)

    def _json_edit_args(self, args: JsonObject) -> JsonObject:
        ref = args.get("base_result_ref")
        if set(args) != {"base_result_ref", "changes"} or not isinstance(ref, str) or ref not in self._complete_reads:
            raise ValueError("use an original complete tools.read_file result from this program")
        receipt = self._complete_reads[ref]
        value = decode_json_object(receipt.canonical["value"])
        path, text = receipt.call.args.get("path"), value.get("text")
        if not isinstance(path, str) or not isinstance(text, str):
            raise ValueError("complete read receipt is missing its path or text")
        try:
            content = replace_json_fields(text, decode_json_object(args["changes"]))
        except ValueError as exc:
            raise ValueError(f"target file {path!r}: {exc}") from exc
        return decode_json_object({"path": path, "content": content, "base_result_ref": ref})
