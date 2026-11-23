from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Protocol

from codey.codemode.identity import rpc_child_admission, rpc_child_allocator, rpc_child_index
from codey.codemode.json_edit import JSON_FIELD_EDIT_GUIDANCE, replace_json_fields
from codey.codemode.navigation import TASK_SOURCE_TOOLS, resolve_task_navigation, resolve_task_source
from codey.codemode.patch import PatchChild, PatchPreflightError, patch_summary
from codey.codemode.types import ToolCatalogEntry
from codey.codemode.verification import resolve_verification
from codey.operations.browser_references import BrowserReferences
from codey.runtime.core.models import ToolCall, ToolResult
from codey.toolchain.constants import STALE_READ_REFERENCE_CODE, STALE_READ_REFERENCE_ERROR
from codey.toolchain.result_schema import validate_output
from codey.toolchain.tool_spec import _check_spec_value_against_schema
from codey.types.json_value import JsonObject, JsonValue, canonical_json_object, decode_json_object, require_json_string


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
        task_source_url: str | None = None,
        browser_references: BrowserReferences | None = None,
        replay_browser_ref: Callable[[int, str, JsonValue], str] | None = None,
        browser_stage: Callable[[], int] | None = None,
        original_browser_wait: Callable[[int], JsonObject | None] | None = None,
        bound_edit: Callable[[int, JsonObject], JsonObject] | None = None,
        plan_patch: Callable[[tuple[JsonObject, ...], tuple[int, ...]], tuple[JsonObject, ...]] | None = None,
        semantic_plan: Callable[[str], tuple[JsonObject, ...]] | None = None,
        symbol_reads: Mapping[str, str] | None = None,
        link_result_observed: Callable[[str, str], bool] | None = None,
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
        self._task_source_url = task_source_url or task_navigation_url
        self._browser_references = browser_references
        self._replay_browser_ref = replay_browser_ref
        self._browser_stage = browser_stage
        self._original_browser_wait = original_browser_wait
        self._bound_edit = bound_edit
        self._plan_patch = plan_patch
        self._semantic_plan = semantic_plan
        self._symbol_reads = dict(symbol_reads or {})
        self._link_result_observed = link_result_observed
        self._next_child = 0
        self._complete_reads: dict[str, ToolResult] = {}
        self._control_snapshot_ref = browser_references.receipt_ref if browser_references is not None else ""

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
        if tool_name == "edit" and "semantic_result_ref" in decoded_args:
            return self._semantic_edit(index, decoded_args)
        if tool_name == "edit" and "files" in decoded_args:
            return self._patch(index, decoded_args)
        if tool_name == "browser_snapshot" and "flow" in decoded_args:
            return self._browser_flow(index, decoded_args)
        json_edit = tool_name == "edit" and "changes" in decoded_args
        click_args, query, steps = self._click_args(tool_name, decoded_args)
        if (canonical_json_object(self._catalog[tool_name].parameters).get("x-codey-result-read") is True
                and not any(step_name == "browser_read" for step_name, _args in steps)):
            steps = (*steps, ("browser_read", JsonObject()))
        step_indices = self._allocate_children(len(steps))
        raw = self._invoke_one(index, tool_name, click_args, query=query)
        link_followup = False
        if (tool_name == "browser_click" and raw.ok
                and raw.audit.get("link_origin") is not None and isinstance(ref := raw.audit.get("result_ref"), str)):
            plans = canonical_json_object(self._catalog[tool_name].parameters).get("x-codey-click-plans")
            if isinstance(plans, dict) and "observed_link" in plans:
                link_steps = self._click_steps(plans["observed_link"], link_ref=ref)
                steps = (*link_steps, *((name, args) for name, args in steps if name == "browser_read"))
                step_indices = (*step_indices, *self._allocate_children(len(steps) - len(step_indices)))
                link_followup = True
        if raw.ok and steps and (error := self._run_click_followups(
                step_indices, steps, query=query, link_followup=link_followup)):
            return {"ok": False, "error": error}
        if not raw.ok:
            error = raw.model_text
            if json_edit and raw.audit.get("error_code") == STALE_READ_REFERENCE_CODE:
                error = f"{STALE_READ_REFERENCE_ERROR} {JSON_FIELD_EDIT_GUIDANCE}"
            return {"ok": False, "error": error}
        value = successful_tool_value(raw, self._catalog[tool_name])
        self._observe_result(tool_name, decoded_args, raw, value)
        output: dict[str, object] = {"ok": True, "value": value}
        if ref := raw.audit.get("result_ref"):
            output["result_ref"] = ref

        return output

    def _run_click_followups(self, indices: tuple[int, ...], steps: tuple[tuple[str, JsonObject], ...], *,
                             query: bool, link_followup: bool) -> str:
        navigation_pending = ""
        for index, (name, args) in zip(indices, steps, strict=True):
            if name == "browser_read" and navigation_pending:
                continue
            try:
                followup = self._invoke_one(index, name, args, query=query)
            except (ValueError, TypeError, RecursionError) as exc:
                error = str(exc)
            else:
                if followup.ok:
                    if (navigation_pending and name == "browser_snapshot" and self._link_result_observed is not None
                            and isinstance(snapshot_ref := followup.audit.get("result_ref"), str)
                            and self._link_result_observed(navigation_pending, snapshot_ref)):
                        navigation_pending = ""
                    continue
                value = followup.canonical.get("value")
                condition = args.get("condition")
                if ((not query or link_followup) and name == "browser_wait"
                        and condition in {"controls_changed", "new_document"}
                        and isinstance(value, dict) and decode_json_object(value).get("reason") == "timeout"):
                    if condition == "new_document":
                        navigation_pending = require_json_string(args.get("value"), "link click reference")
                    continue
                error = followup.model_text
            return (f"Click delivered; {name} failed: {error}. "
                    "Continue from the committed receipts; do not repeat the click.")
        return "Click delivered; the destination document has not been observed yet." if navigation_pending and query else ""

    def _observe_result(self, tool_name: str, args: JsonObject, raw: ToolResult, value: object) -> None:
        if ref := raw.audit.get("result_ref"):
            if tool_name == "browser_snapshot" and isinstance(ref, str) and isinstance(value, dict):
                snapshot = decode_json_object(value)
                scope = snapshot.get("scope")
                if (snapshot.get("elements_truncated") is False and isinstance(scope, dict)
                        and decode_json_object(scope).get("root_ref") == "" and decode_json_object(scope).get("selector") == ""):
                    self._control_snapshot_ref = ref
                    document = raw.audit.get("browser_document")
                    if (not raw.truncated and isinstance(document, dict)
                            and (snapshot.get("complete") is True or snapshot.get("text_truncated") is True)):
                        self._browser_references = BrowserReferences.observe(ref, snapshot, decode_json_object(document))
            if (tool_name == "read_file" and "edit" in self._catalog
                    and isinstance(ref, str) and isinstance(value, dict) and not raw.truncated):
                data = decode_json_object(value)
                path, text = args.get("path"), data.get("text")
                if (isinstance(path, str) and isinstance(text, str) and data.get("text_complete") is True
                        and data.get("start_line") == 1 and data.get("next_offset") == 0
                        and data.get("end_line") == data.get("total_lines")):
                    self._complete_reads[ref] = raw

    def _patch(self, index: int, args: JsonObject) -> JsonObject:
        contract = canonical_json_object(self._catalog["edit"].parameters).get("x-codey-patch")
        files = args.get("files")
        if contract is None or self._plan_patch is None or self._bound_edit is not None or not isinstance(files, list):
            return {"ok": False, "error": "Multi-file patch is unavailable for this edit contract."}
        if error := _check_spec_value_against_schema("edit", "args", contract, args, strict=True):
            return {"ok": False, "error": error}
        indices = (index, *self._allocate_children(len(files) - 1))
        rows = tuple(decode_json_object(row) for row in files)
        return self._apply_patch(rows, indices)

    def _apply_patch(self, rows: tuple[JsonObject, ...], indices: tuple[int, ...]) -> JsonObject:
        assert self._plan_patch is not None
        paths = tuple(require_json_string(row.get("path"), "patch path") for row in rows)
        try:
            planned = self._plan_patch(rows, indices)
        except (OSError, ValueError) as exc:
            failed = exc.path if isinstance(exc, PatchPreflightError) else ""
            return {"ok": False, "error": f"Patch preflight stopped before new writes: {exc}",
                    "patch": patch_summary(paths, {}, failed_path=failed)}
        written: list[str] = []
        statuses: list[str] = []
        receipts: dict[str, PatchChild] = {}
        for path, child_index, row in zip(paths, indices, planned, strict=True):
            receipt = self._invoke_one(child_index, "edit", row, query=False)
            ref = receipt.audit.get("result_ref")
            receipts[path] = PatchChild(receipt, ref if isinstance(ref, str) else "")
            if not receipt.ok:
                return {"ok": False, "error": f"Patch stopped after writing {', '.join(written) or 'no files'}. "
                    f"{receipt.model_text} Read the failed and remaining files, then continue only the missing edits. "
                    "The committed child receipts retain the written versions; do not repeat completed writes.",
                    "patch": patch_summary(paths, receipts)}
            if receipt.audit.get("changed") is True:
                written.append(path)
            statuses.append(receipt.model_text)
        return {"ok": True, "value": "\n".join(statuses), "patch": patch_summary(paths, receipts)}

    def _semantic_edit(self, index: int, args: JsonObject) -> JsonObject:
        contract = canonical_json_object(self._catalog["edit"].parameters).get("x-codey-semantic-edit")
        if contract is None or self._semantic_plan is None or self._plan_patch is None or self._bound_edit is not None:
            return {"ok": False, "error": "Semantic edits are unavailable for this contract"}
        if error := _check_spec_value_against_schema("edit", "args", contract, args, strict=True):
            return {"ok": False, "error": error}
        ref = args["semantic_result_ref"]
        assert isinstance(ref, str)
        try:
            rows = self._semantic_plan(ref)
            reads = (index, *self._allocate_children(len(rows) - 1))
            files: list[JsonObject] = []
            for position, row in zip(reads, rows, strict=True):
                receipt = self._invoke_one(position, "read_file", {"path": row["path"]}, query=False)
                if not receipt.ok or receipt.canonical.get("file_version") != "raw:" + str(row["expected_version"]):
                    raise ValueError("semantic plan is stale; query the current workspace again")
                files.append({"path": row["path"], "replacements": row["replacements"],
                              "base_result_ref": require_json_string(receipt.audit["result_ref"], "read result_ref")})
            indices = self._allocate_children(len(files))
            return self._apply_patch(tuple(files), indices)
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": "Semantic patch preflight failed: " + str(exc)}

    def _browser_flow(self, index: int, args: JsonObject) -> JsonObject:
        from codey.codemode.browser_flow import flow_steps

        contract = canonical_json_object(self._catalog["browser_snapshot"].parameters).get("x-codey-browser-flow")
        if not isinstance(contract, dict):
            return {"ok": False, "error": "Browser flow is unavailable for this catalog."}
        steps = flow_steps(args, contract)
        indices = (index, *self._allocate_children(len(steps) - 1))
        observed: list[JsonValue] = []
        for child_index, (name, step_args) in zip(indices, steps, strict=True):
            receipt = self._invoke_one(child_index, name, step_args, query=False)
            if not receipt.ok:
                return {"ok": False, "error": f"Browser flow stopped at {name}: {receipt.model_text}", "steps": observed}
            value = successful_tool_value(receipt, self._catalog[name])
            self._observe_result(name, step_args, receipt, value)
            observed.append({"tool": name, "result_ref": str(receipt.audit.get("result_ref") or ""),
                "value": decode_json_object({"value": value})["value"]})
        return {"ok": True, "value": {"steps": observed}}

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
        if tool_name == "browser_navigate":
            decoded_args = resolve_task_navigation(decoded_args, self._task_navigation_url)
        if tool_name in TASK_SOURCE_TOOLS:
            decoded_args = resolve_task_source(decoded_args, self._task_source_url)
        if tool_name == "run":
            decoded_args = resolve_verification(decoded_args, canonical_json_object(self._catalog[tool_name].parameters))
        if tool_name == "edit" and "symbol" in decoded_args:
            from codey.operations.edit_base import bind_symbol_read, symbol_read_refs

            decoded_args = bind_symbol_read(decoded_args, {
                **self._symbol_reads, **symbol_read_refs(self._complete_reads, self._complete_reads),
            })
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
        if self._catalog[tool_name].namespace != "browser" or not isinstance(selector := args.get("ref"), dict):
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
                or purpose == "observed_link" or set(args) != {"ref", "purpose"}):
            raise ValueError("The requested click follow-up is unavailable or its arguments are invalid.")
        return {"ref": args["ref"]}, purpose == "result", self._click_steps(
            plans[purpose], bind_controls=purpose == "controls")

    def _click_steps(self, steps: JsonValue, *, link_ref: str = "",
                     bind_controls: bool = False) -> tuple[tuple[str, JsonObject], ...]:
        if not isinstance(steps, list):
            raise ValueError("The requested click follow-up is unavailable or its arguments are invalid.")
        parsed: list[tuple[str, JsonObject]] = []
        for item in steps:
            if not isinstance(item, dict) or set(item) != {"name", "args"}:
                raise ValueError("The requested click follow-up is unavailable or its arguments are invalid.")
            step_name = item["name"]
            if not isinstance(step_name, str) or step_name not in self._catalog:
                raise ValueError("The requested click follow-up contains an unavailable tool.")
            step_args = decode_json_object(item["args"])
            if bind_controls and step_name == "browser_wait" and step_args.get("condition") == "controls_changed":
                if not self._control_snapshot_ref:
                    continue
                step_args["value"] = self._control_snapshot_ref
            if step_name == "browser_wait" and link_ref:
                step_args["value"] = link_ref
            parsed.append((step_name, step_args))
        return tuple(parsed)

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
