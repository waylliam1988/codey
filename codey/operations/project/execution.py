"""Execute project tools against captured workspace evidence."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from codey.operations.kernel_result import result_from_outcome
from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import (
    JsonObject,
    JsonValue,
    ToolArgs,
    decode_json_object,
    require_json_integer,
    require_json_string,
)

if TYPE_CHECKING:
    from codey.agents.request import ChangeTracker
    from codey.agents.tools import AgentToolFns
    from codey.automation.browser_session import BrowserSession
    from codey.operations.task_execution import ProjectPolicyCheck
    from codey.operations.task_session import TaskSession
    from codey.toolchain.runtime import ToolOutcome



class ProjectExecutor:
    def __init__(self, *, session: TaskSession | None, project_path: Path | None, tool_fns: AgentToolFns | None,
                 browser_session: BrowserSession | None, change_tracker: ChangeTracker | None,
                 permission_profile: str, check_policy: Callable[[ToolCall], ProjectPolicyCheck]) -> None:
        self.session = session
        self.project_path = project_path
        self.tool_fns = tool_fns
        self.browser_session = browser_session
        self.change_tracker = change_tracker
        self.permission_profile = permission_profile
        self.check_policy = check_policy

    def execute(self, call: ToolCall) -> tuple[ToolResult, bool, int | None]:
        if self.project_path is None:
            return ToolResult(ok=False, call=call, model_text="ERROR: project path unavailable"), False, None
        if self.tool_fns is None:
            return ToolResult(ok=False, call=call, model_text="ERROR: project tools unavailable"), False, None
        project_path = self.project_path
        check = self.check_policy(call)
        if check.denied:
            from codey.operations.kernel_result import policy_denial_result

            result = policy_denial_result(call, check.message, reason_code=check.reason_code)
            return result, False, None
        name = (call.name or "").strip().lower()
        runtime_name = {"list_dir": "ls", "read_file": "read", "grep": "search",
                        "find_references": "references"}.get(name, name)
        try:
            if name == "syntax_query":
                from codey.toolchain.syntax_query import syntax_query

                result = result_from_outcome(call, syntax_query(project_path,
                    require_json_string(call.args.get("path"), "path"), require_json_string(call.args.get("node_type"), "node_type"),
                    limit=require_json_integer(call.args.get("limit", 40), "limit")))
                return result, result.ok, None
            if name == "workspace_diff":
                from codey.workspace.changes import ChangeTracker
                from codey.workspace.task_changes import collect_task_changes

                tracker = self.change_tracker
                if not isinstance(tracker, ChangeTracker):
                    result = ToolResult(call, "Workspace snapshot is unavailable for this task.", ok=False)
                    return result, False, None
                payload = decode_json_object(collect_task_changes(project_path, tracker))
                ok = payload.get("ok") is True
                result = ToolResult(call, str(payload.get("diff") or payload.get("error") or "No task-owned changes."),
                                    ok=ok, canonical={"workspace_changes": payload})
                return result, ok, None
            if name == "semantic_definition":
                from codey.toolchain.semantic_definition import semantic_definition

                result = result_from_outcome(call, semantic_definition(project_path,
                    require_json_string(call.args.get("path"), "path"), require_json_integer(call.args.get("line", 1), "line"),
                    require_json_string(call.args.get("symbol", ""), "symbol"),
                    operation=require_json_string(call.args.get("operation", "definition"), "operation"),
                    new_name=require_json_string(call.args.get("new_name", ""), "new_name")))
                return result, result.ok, None
            if runtime_name in {"ls", "read", "search", "references"}:
                from codey.agents.tool_execution import execute_information_tool_call

                outcome = execute_information_tool_call(
                    project_path, self.tool_fns,
                    ToolCall(runtime_name, dict(call.args or {})),
                )
                result = result_from_outcome(call, outcome)
                return result, result.ok, None
            if name == "edit":
                outcome = self._execute_edit(call)
                result = result_from_outcome(call, outcome)
                return result, result.ok, None
            if name == "run":
                from dataclasses import replace

                from codey.toolchain.command_output import output_tail, tail_lines

                tail = tail_lines((call.args or {}).get("output_tail_lines"))
                outcome = self.tool_fns.execute_run_command(
                    project_path, str((call.args or {}).get("path", ".") or "."),
                    str((call.args or {}).get("command", "") or ""),
                    permission_profile=self.permission_profile, phase="writer",
                    tool_id=call.call_id,
                )
                if tail is not None:
                    outcome = replace(outcome, model_text=f"Exit code: {outcome.exit_code}\n" + output_tail(outcome.model_text, tail),
                                      presentation={**outcome.presentation, "output_tail_lines": tail})
                result = result_from_outcome(call, outcome)
                return result, result.ok, outcome.exit_code
        except Exception as exc:
            result = ToolResult(ok=False, call=call, model_text=f"ERROR: {exc}")
            return result, False, None
        result = ToolResult(ok=False, call=call, model_text=f"ERROR: unsupported project tool {name}")
        return result, False, None

    def _execute_edit(self, call: ToolCall) -> ToolOutcome:
        from codey.agents.protocol import canonical_project_path
        from codey.agents.tool_execution import read_before_edit_outcome
        from codey.toolchain.runtime import ToolOutcome, replacement_blocks, safe_join

        if self.project_path is None:
            return ToolOutcome.error("project path unavailable")
        if self.tool_fns is None:
            return ToolOutcome.error("project tools unavailable")
        project_path = self.project_path
        args = dict(call.args or {})
        path = str(args.get("path", "") or "")
        try:
            canonical = canonical_project_path(project_path, path)
            safe_join(project_path, canonical)
        except ValueError:
            return ToolOutcome.error("workspace_escape: path escapes project root")
        if "browser_source" in args:
            prepared = self._browser_copy_content(path, canonical, args)
            if isinstance(prepared, ToolOutcome):
                return prepared
            from codey.workspace.paths import content_hash

            outcome = self._write_content(project_path, path, canonical, {**args, "content": prepared})
            return (replace(outcome, audit={**outcome.audit, "browser_copy_content_hash": content_hash(prepared)})
                    if outcome.ok else outcome)
        if args.get("declared_fields") is True:
            return self._declared_field_edit(project_path, path, canonical, args)
        symbol_only = "symbol" in args
        if symbol_only:
            from codey.operations.project.edit_base import symbol_edit_args

            try:
                args = symbol_edit_args(self.session, project_path, canonical, args)
            except ValueError as exc:
                return ToolOutcome.error(str(exc))
        if "content" in args:
            return self._write_content(project_path, path, canonical, args, symbol_only=symbol_only)
        from codey.operations.file_field_binding import file_rules

        if self.session is not None and file_rules(self.session.policy.required_checks, canonical):
            return ToolOutcome.error("Receipt-bound fields require content or declared_fields=true with a complete version-bound read")
        guard = read_before_edit_outcome(
            project_path, path, set(self.session.read_files) if self.session is not None else set(),
        )
        if guard is not None:
            return guard
        # Canonical protocol only: replacements with old_string/new_string.
        # Top-level aliases are rejected before any file write.
        for legacy in ("search", "replace"):
            if legacy in args:
                return ToolOutcome.error(f"edit rejects legacy alias: {legacy}")
        try:
            edit_blocks = replacement_blocks(args.get("replacements"))
        except ValueError as exc:
            return ToolOutcome.error(str(exc))
        if self.change_tracker is not None:
            self.change_tracker.capture_before(path)
        from codey.operations.project.project_delivery import documented_record_fields, record_field_error

        version = args.get("expected_version")
        fields = documented_record_fields(self.session, canonical)
        from codey.operations.python_edit_assistance import python_edit_transform, render_python_edit

        transform = python_edit_transform(self.session, project_path, canonical) if not fields else None
        if isinstance(version, str) or fields or transform is not None:
            from codey.toolchain.runtime import edit_file_at_version

            def validator(content: str) -> str:
                return record_field_error(canonical, fields, content)

            outcome = edit_file_at_version(project_path, path, edit_blocks, version=version if isinstance(version, str) else "",
                                           validate_content=validator if fields else None, transform_content=transform)
            outcome = render_python_edit(outcome)
        else:
            outcome = self.tool_fns.edit_file(project_path, path, edit_blocks)
        if outcome.ok and outcome.changed and self.change_tracker is not None:
            self.change_tracker.capture_after(path)
        return outcome

    def _declared_field_edit(self, project: Path, path: str, canonical: str, args: ToolArgs) -> ToolOutcome:
        from codey.operations.file_field_binding import bound_content
        from codey.operations.project.edit_base import edit_base_text
        from codey.toolchain.runtime import ToolOutcome

        if self.session is None or self.browser_session is None:
            return ToolOutcome.error("Receipt-bound file fields require the authorized browser session")
        baseline = edit_base_text(self.session, project, canonical,
                                  require_json_string(args.get("base_result_ref"), "base_result_ref"))
        prepared = bound_content(self.session, canonical, baseline, self.browser_session.document_identity())
        return self._write_content(project, path, canonical, {**args, "content": prepared})

    def _write_content(self, project: Path, path: str, canonical: str, args: ToolArgs, *,
                       symbol_only: bool = False) -> ToolOutcome:
        from codey.operations.project.edit_base import content_edit_version

        assert self.tool_fns is not None
        version = content_edit_version(self.session, project, canonical, args)
        if self.change_tracker is not None:
            self.change_tracker.capture_before(path)
        content = str(args.get("content") or "")
        from codey.operations.file_field_binding import validate_content
        from codey.operations.project.project_delivery import documented_record_fields, record_field_error

        if self.session is not None:
            fields = documented_record_fields(self.session, canonical)
            error = (record_field_error(canonical, fields, content) if fields else "") or validate_content(
                self.session.policy.required_checks, canonical, content,
                self.session.executed, self.session._memory_results,
                self.browser_session.document_identity() if self.browser_session is not None else None)
            if error:
                from codey.toolchain.runtime import ToolOutcome

                return ToolOutcome.error(error, error_code="file_field_mismatch")
        assistance: JsonObject = {}
        if symbol_only:
            from codey.operations.project.edit_base import edit_base_text
            from codey.operations.python_edit_assistance import python_edit_transform

            if transform := python_edit_transform(self.session, project, canonical, allow_import=False):
                original = edit_base_text(self.session, project, canonical,
                                          require_json_string(args["base_result_ref"], "base_result_ref"))
                content, assistance = transform(original, content)
        outcome = (self.tool_fns.write_file(project, path, content) if version is None else
                   self.tool_fns.write_file(project, path, content, expected_version=version))
        if assistance and outcome.ok:
            from codey.operations.python_edit_assistance import render_python_edit

            outcome = render_python_edit(replace(outcome, audit={**outcome.audit, **assistance}))
        if outcome.ok and outcome.changed and self.change_tracker is not None:
            self.change_tracker.capture_after(path)
        return outcome

    def _browser_copy_content(self, path: str, canonical: str, args: ToolArgs) -> str | ToolOutcome:
        from codey.codemode.json_edit import JsonNumber, replace_json_fields
        from codey.operations.browser.browser_copy import (
            CAPTURE,
            EXPIRED,
            captured_data,
            copy_target,
            copy_turn,
            select_source,
        )
        from codey.operations.project.edit_base import edit_base_text
        from codey.task.web_intent import source_url_key
        from codey.toolchain.runtime import ToolOutcome

        session, browser, project = self.session, self.browser_session, self.project_path
        if session is None or browser is None or project is None:
            return ToolOutcome.error("Browser copy requires a project and an authorized browser.")
        target = copy_target(session.task_text)
        if target is None or path != target.path or canonical != target.path:
            return ToolOutcome.error("Browser copy cannot write outside the task's target.")
        source = decode_json_object(args["browser_source"])
        ref = require_json_string(source.get("ref"), "browser source reference")
        with session.projection_lock:
            results = dict(session._memory_results)
        browser.prepare_from_receipts(results.values())
        current = copy_turn(target, session.task_text, results)
        receipt = results.get(ref)
        document = browser.document_identity()
        if (current.ref != ref or current.phase != "edit" or receipt is None or document is None
                or source_url_key(str(document.get("url") or "")) != source_url_key(target.url)
                or receipt.audit.get("browser_document") != document):
            return ToolOutcome.error("The browser observation is no longer current. Observe the authorized page again.", error_code=EXPIRED)
        data = captured_data(receipt)
        if data is None:
            return ToolOutcome.error("The browser observation is incomplete. Observe the page again.", error_code=EXPIRED)
        fresh = browser.evaluate(CAPTURE)
        if (not fresh.ok or fresh.observation.get("truncated") is not False
                or fresh.observation.get("value") != data or browser.document_identity() != document):
            return ToolOutcome.error("The webpage data changed. Observe the authorized page again.", error_code=EXPIRED)
        selectors = (decode_json_object(source["selectors"]) if target.additional_fields else
                     {target.field: source["selector"]})
        if set(selectors) != {field for field, _ in target.fields}:
            return ToolOutcome.error("Select exactly the fields declared by the task.")
        changes: dict[str, JsonValue | JsonNumber] = {}
        for field, value_type in target.fields:
            selector = decode_json_object(selectors[field])
            if current.business_key and "key" in selector and selector["key"] != current.business_key:
                return ToolOutcome.error("Select the row key specified by the task.")
            changes[field] = select_source(data, selector, value_type)
        baseline = edit_base_text(session, project, canonical, require_json_string(args["base_result_ref"], "base_result_ref"))
        return replace_json_fields(baseline, changes)
