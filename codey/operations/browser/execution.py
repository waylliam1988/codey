"""Execute browser tools under the task's captured document and receipt context."""
from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Protocol

from codey.automation.browser_contracts import BrowserAction
from codey.automation.browser_document import same_document
from codey.automation.browser_receipts import delivery
from codey.operations.browser import browser_followups, browser_text_report, browser_upload
from codey.operations.kernel_provenance import _kernel_workspace_identity_of, _with_trusted_workspace_state
from codey.operations.research.research_submission_handoff import traces_workflow_document
from codey.policies.browser_requirement import PAGE_TRANSITIONS, browser_receipt_tools
from codey.policies.tool_requirement import has_browser_field_bindings
from codey.runtime.core import cancellation
from codey.runtime.core.models import ToolCall, ToolResult
from codey.toolchain.constants import PAGE_DETAIL_TOOL
from codey.types.json_value import JsonObject, decode_json_object, require_json_integer, require_json_string
from codey.types.tool_image import ToolImageRef

if TYPE_CHECKING:
    from codey.automation.browser_session import BrowserSession, BrowserToolOutput
    from codey.operations.task_session import TaskSession
    from codey.storage.managed_outputs import ManagedOutputStore


class ReadBrowserSource(Protocol):
    def __call__(self, browser: BrowserSession, call: ToolCall, *, turn: int,
                 tool_index: int) -> tuple[ToolResult, bool, int | None]: ...


class BrowserExecutor:
    def __init__(self, *, session: TaskSession | None, browser_session: BrowserSession | None,
                 wait_document: JsonObject | None, permission_profile: str,
                 managed_outputs: ManagedOutputStore | None, session_id: str, run_id: str,
                 trace_research: bool, read_source: ReadBrowserSource) -> None:
        self.session = session
        self.browser_session = browser_session
        self._browser_wait_document = wait_document
        self.permission_profile = permission_profile
        self.managed_outputs = managed_outputs
        self.session_id = session_id
        self.run_id = run_id
        self.trace_research = trace_research
        self.read_source = read_source

    def execute(self, call: ToolCall, *, turn: int, tool_index: int) -> tuple[ToolResult, bool, int | None]:
        browser = self.browser_session
        if browser is None:
            return ToolResult(ok=False, call=call, model_text="ERROR: browser session unavailable"), False, None
        try:
            assert self.session is not None
            with self.session.projection_lock:
                browser.prepare_from_receipts(tuple(self.session._memory_results.values()))
            checks = self.session.policy.required_checks
            from codey.operations.browser.browser_copy import CAPTURE

            trace_document = (call.name in browser_receipt_tools(checks)
                              or self.session.task_kind in {"chat", "hybrid"}
                              or traces_workflow_document(self.session, call.name)
                              or self.trace_research
                              or (call.name in browser_text_report.METADATA_TOOLS and browser_text_report.report_scope(self.session) is not None)
                              or (call.name in browser_upload.UPLOAD_RECEIPTS and browser_upload.upload_scope(self.session) is not None)
                              or call.name in {"browser_click", "browser_screenshot", "browser_snapshot", "browser_locate", "browser_inspect"}
                              or (call.name == "browser_eval" and call.args.get("code") == CAPTURE) or
                              (call.name in {"browser_eval", "browser_read"} and has_browser_field_bindings(checks, tool=call.name)))
            transition = call.name in PAGE_TRANSITIONS
            before = browser.document_identity() if trace_document and not transition else None
            if call.name == "browser_click" and isinstance(ref := call.args.get("ref"), str):
                prior = browser_followups.prior_single_click(self.session.task_text, self.session._memory_results, ref, before)
                if prior:
                    return ToolResult(call, f"Click {prior} was already dispatched under the user's once-only instruction. "
                                      "Wait or observe the current page without repeating this control.", ok=False,
                                      canonical={"value": {"delivery": "not_delivered", "prior_click": prior}}), False, None
            result, ok, code = self.execute_operation(browser, call, turn=turn, tool_index=tool_index)
            if trace_document and (transition or before is not None):
                try:
                    after = browser.document_identity()
                except (RuntimeError, ValueError, OSError) as exc:
                    after = None
                    result = replace(result, audit={**result.audit, "browser_observation_error": str(exc)})
                if after is not None and (transition or same_document(before, after)):
                    result = replace(result, audit={**result.audit, "browser_document": after})
        except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
            raise
        except Exception as exc:
            result, ok, code = ToolResult(ok=False, call=call, model_text=f"ERROR: {exc}"), False, None
        try:
            binding = browser.resource_binding()
        except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
            raise
        except Exception as exc:
            return replace(result, ok=False, model_text=result.model_text + f"\nERROR: browser resource identity unavailable: {exc}"), False, code
        if binding is not None:
            result = replace(result, audit={**result.audit, "browser_resource": binding})
        return result, ok, code

    def execute_operation(self, browser: BrowserSession, call: ToolCall, *, turn: int,
                                   tool_index: int) -> tuple[ToolResult, bool, int | None]:
        try:
            if call.name in {"browser_read", PAGE_DETAIL_TOOL}:
                return self.read_source(browser, call, turn=turn, tool_index=tool_index)
            output = self._browser_action(browser, call)
            images = self._persist_browser_image(output) if call.name == "browser_screenshot" and output.ok else ()
            if self.session is not None:
                from codey.operations.browser.browser_references import reference_failure

                output = reference_failure(output, self.session._memory_results)
            value = dict(output.observation)
            baseline = value.pop("query_baseline", None)
            link_origin = value.pop("link_origin", None)
            value["text"] = value.get("text", output.model_text)
            audit: JsonObject = {"query_baseline": baseline} if baseline is not None else {}
            if link_origin is not None:
                audit["link_origin"] = link_origin
            if (call.name == "browser_wait" and output.ok and self.session is not None
                    and browser_followups.pending_click(self.session._memory_results, browser.document_identity()) is not None):
                audit["query_wait_document"] = browser.document_identity()
            result = ToolResult(ok=output.ok, call=call, model_text=output.model_text,
                                canonical={"value": value}, audit=audit, images=images)
            return result, result.ok, None
        except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
            raise
        except Exception as exc:
            return ToolResult(ok=False, call=call, model_text=f"ERROR: {exc}"), False, None

    def _browser_action(self, browser: BrowserSession, call: ToolCall) -> BrowserToolOutput:
        args = call.args
        ref = str(args.get("ref") or "")
        if call.name == "browser_snapshot":
            file_inputs_only = args.get("file_inputs_only") is True
            if file_inputs_only:
                browser_upload.verified_upload_document(self.session, browser)
            return browser.snapshot(max_elements=require_json_integer(args.get("max_elements", 80), "max_elements"),
                                    max_text=require_json_integer(args.get("max_text", 12000), "max_text"),
                                    file_inputs_only=file_inputs_only,
                                    scope_ref=require_json_string(args.get("scope_ref", ""), "scope_ref"),
                                    selector=require_json_string(args.get("selector", ""), "selector"),
                                    base_revision=require_json_integer(args["base_revision"], "base_revision") if "base_revision" in args else None)
        if call.name == "browser_click":
            from codey.automation.browser_session import BrowserToolOutput
            from codey.operations.browser.browser_linked_record import linked_record_click_allowed
            from codey.operations.browser.browser_waits import observed_link_outcome, pending_link_click

            document = browser.document_identity() if self.session is not None else None
            if self.session is not None and not linked_record_click_allowed(self.session, ref, document):
                return BrowserToolOutput("The click is outside the verified first record or was already delivered.", False,
                    {"action": "click", "ref": ref, "delivery": "not_delivered"})
            if self.session is not None and (prior := pending_link_click(self.session, ref, document)):
                return BrowserToolOutput("The link click was already delivered. Observe the current document.",
                    False, {"action": "click", "ref": ref, "delivery": "not_delivered", "prior_click": prior})
            if (self.session is not None and (pending := browser_followups.pending_click(
                    self.session._memory_results, document)) is not None
                    and not observed_link_outcome(self.session, pending[0], document)
                    and browser.matches_query_control(ref, decode_json_object(pending[1].audit["query_baseline"]))):
                message = (browser_followups.UNCERTAIN if delivery(pending[1]) == "uncertain"
                           else browser_followups.PENDING)
                return BrowserToolOutput(message, False, {"action": "click", "ref": ref, "delivery": "not_delivered"})
            return browser.click(ref, query=browser_followups.query_click.get(),
                                 result_ref=require_json_string(args.get("result_ref", ""), "result_ref"))
        if call.name == "browser_fill":
            return browser.fill(ref, str(args.get("value") or ""))
        if call.name == "browser_scroll":
            return browser.action(BrowserAction.SCROLL, args)
        if call.name == "browser_wait":
            if args.get("condition") in {"text_changed", "controls_changed"}:
                return self._wait_for_observed_text(browser, call)
            if args.get("condition") == "new_document" and args.get("value"):
                from codey.operations.browser.browser_waits import clicked_link_origin

                origin = clicked_link_origin(self.session, require_json_string(args["value"], "click receipt"))
                return browser.wait("new_document", "", require_json_integer(args["timeout_ms"], "timeout_ms"),
                                    expected_document=origin)
            if self._browser_wait_document is not None:
                return browser.wait(str(args.get("condition") or ""), str(args.get("value") or ""),
                    require_json_integer(args.get("timeout_ms"), "timeout_ms"),
                    expected_document=self._browser_wait_document)
            return browser.wait(str(args.get("condition") or ""), str(args.get("value") or ""),
                                require_json_integer(args.get("timeout_ms"), "timeout_ms"))
        if call.name == "browser_navigate":
            return browser.navigate(str(args.get("url") or ""))
        if call.name == "browser_eval":
            return browser.evaluate(str(args.get("code") or ""))
        if call.name == "browser_tab_list":
            return browser.tab_list()
        if call.name == "browser_tab_new":
            return browser.tab_new(str(args.get("url") or ""))
        if call.name == "browser_tab_switch":
            return browser.tab_switch(require_json_integer(args.get("index"), "index"))
        if call.name == "browser_upload":
            return browser_upload.select_attachment(self.session, browser, self.managed_outputs, self.session_id, args)
        if call.name == "browser_download":
            return self._browser_download(browser, ref)
        return browser.action(BrowserAction(call.name.removeprefix("browser_")), args)

    def _wait_for_observed_text(self, browser: BrowserSession, call: ToolCall) -> BrowserToolOutput:
        from codey.operations.browser.browser_waits import snapshot_wait_baseline

        if self.session is None:
            raise ValueError("text_changed requires the original task receipts")
        condition = require_json_string(call.args.get("condition"), "condition")
        baseline, document = snapshot_wait_baseline(self.session, require_json_string(call.args.get("value"), "value"), condition)
        if self._browser_wait_document is not None and not same_document(self._browser_wait_document, document):
            raise ValueError("text_changed snapshot does not belong to the original background wait document")
        timeout_ms = require_json_integer(call.args.get("timeout_ms"), "timeout_ms")
        if condition == "controls_changed":
            if not isinstance(baseline, list):
                raise ValueError("controls_changed requires observed controls")
            return browser.wait_for_control_change(baseline, document, timeout_ms)
        return browser.wait_for_text_change(require_json_string(baseline, "baseline text"), document, timeout_ms)

    def _persist_browser_image(self, output: BrowserToolOutput) -> tuple[ToolImageRef, ...]:
        if self.managed_outputs is None or not self.session_id or not self.run_id:
            raise ValueError("task-scoped image storage unavailable")
        image = decode_json_object(output.observation["image"])
        stored = self.managed_outputs.write_binary_artifact(session_id=self.session_id, run_id=self.run_id,
            raw=output.binary, permission_profile=self.permission_profile)
        if image["sha256"] != stored.sha256:
            raise ValueError("captured image changed during persistence")
        return (ToolImageRef(str(self.managed_outputs.root.parent), self.session_id, self.run_id, stored.handle,
            stored.sha256, stored.stored_bytes, require_json_integer(image["width"], "width"),
            require_json_integer(image["height"], "height"), require_json_string(image["observation_ref"], "observation_ref")),)

    def _browser_download(self, browser: BrowserSession, ref: str) -> BrowserToolOutput:
        if self.managed_outputs is None or not self.session_id or not self.run_id:
            raise ValueError("task-scoped download storage unavailable")
        output = browser.download(ref)
        if not output.ok:
            return output
        try:
            stored = self.managed_outputs.write_binary_artifact(
                session_id=self.session_id, run_id=self.run_id,
                raw=output.binary, permission_profile=self.permission_profile)
        except (OSError, ValueError) as exc:
            return replace(output, ok=False, model_text=f"ERROR: download delivered but persistence failed: {exc}",
                           observation={"action": "download", "delivery": "delivered", "storage": "failed"})
        return replace(output, model_text=f"Download retained: {output.observation['suggested_filename']} "
                       f"({stored.stored_bytes} bytes, SHA256 {stored.sha256})",
                       observation={**output.observation, "path": str(stored.path), "handle": stored.handle,
                                    "bytes": stored.stored_bytes, "sha256": stored.sha256})


def browser_execution_receipt(result: ToolResult, identity: str, elapsed_ms: int) -> ToolResult:
    workspace_proof = _kernel_workspace_identity_of(result)
    raw = result.canonical.get("value")
    delivery = decode_json_object(raw).get("delivery") if isinstance(raw, dict) else None
    observed = replace(result, audit={**result.audit, "browser_execution": {
        "effect_ref": identity, "elapsed_ms": elapsed_ms,
        "phase": "unknown" if delivery == "uncertain" else "finished"}})
    if workspace_proof is not None:
        return _with_trusted_workspace_state(observed, revision=workspace_proof.revision,
                                            fingerprint=workspace_proof.fingerprint)
    return observed
