"""Production tool execution for the unified kernel (operations layer).

Project tools reuse the coding guards and readers (path safety, command
policy, shell approval surface); Research tools reuse ResearchTools, the
source gateway, and the evidence ledger. Completion facts come only from
successful tool results and ledger
state, never from substring matching or model-supplied parameters.
"""

from __future__ import annotations

import contextlib
import copy
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, cast

from codey.policies.browser_requirement import browser_receipt_tools
from codey.policies.tool_requirement import has_browser_field_bindings
from codey.research.ledger import ResearchLedger
from codey.research.ledger_receipts import ledger_observation, source_search_observation
from codey.runtime.core import cancellation
from codey.runtime.core.execution_record import HitTarget
from codey.runtime.core.models import ToolCall, ToolResult
from codey.toolchain.tool_spec import _CONTROLLER_ALIAS_ID_ARG, SOURCE_READ_TOOL_NAMES, is_source_read_tool
from codey.types.json_value import JsonObject, JsonValue, ToolArgs, require_json_integer

if TYPE_CHECKING:
    from codey.agents.request import ChangeTracker
    from codey.agents.tools import AgentToolFns
    from codey.automation.browser_session import BrowserSession, BrowserToolOutput
    from codey.knowledge.store import KnowledgeStore
    from codey.operations.kernel_contracts import ToolExecutor
    from codey.operations.task_session import TaskSession
    from codey.operations.tool_result_reader import ReceiptReaderContext
    from codey.research.answer_contract import AnswerScope
    from codey.research.output_receipts import _Outcome
    from codey.research.receipt_types import SourceSearchObservation
    from codey.research.source_gateway import _SearchProvider
    from codey.storage.managed_outputs import ManagedOutputStore
    from codey.toolchain.runtime import ToolOutcome
    from codey.toolchain.tool_spec import ToolSpec
from codey.research.tools import ResearchToolOutput, ResearchTools


def close_research_tools(tools: ResearchTools | None) -> None:
    """Release an owned search session without masking the task outcome."""
    close = getattr(getattr(tools, "search", None), "close", None)
    if callable(close):
        with contextlib.suppress(Exception):
            close()


def bind_browser_session(resources: ResearchTools) -> None:
    """Attach the shared page resource once, for every production kernel entry."""
    if resources.browser_session is None:
        from codey.automation.browser_session import BrowserSession

        resources.browser_session = BrowserSession.from_provider(resources.search)


def build_research_tools(deps: object, *, session_id: str, project: str, browser_sources: bool = True,
                         browser_actions: bool = False) -> ResearchTools:
    """Build the execution resources; configuration errors propagate to entry."""
    from codey.knowledge.changes import KnowledgeChanges
    from codey.research.tools import ResearchTools

    knowledge_store = cast("KnowledgeStore | None", getattr(deps, "knowledge_store", None))
    search_factory = cast("Callable[[], _SearchProvider] | None", getattr(deps, "search_factory", None))
    if search_factory is None:
        from codey.research.search_factory import default_research_search_provider
        from codey.research.web_sources import WebSourceProvider

        search_factory = default_research_search_provider if browser_sources or browser_actions else WebSourceProvider
    search = search_factory()
    try:
        resources = ResearchTools(
            search=search, store=knowledge_store,
            changes=KnowledgeChanges(root=knowledge_store.root) if knowledge_store is not None else None,
            diagnostics=None, session_id=session_id, project=project,
        )
        if browser_actions:
            bind_browser_session(resources)
        return resources
    except BaseException:
        close = getattr(search, "close", None)
        if callable(close):
            with contextlib.suppress(Exception):
                close()
        raise


def effective_project_profile(permission_profile: object) -> str:
    """Project-guard profile shared by execution and shell approval.

    The Research profile controls research context and source tools. A
    project tool still needs the coding path/command guard; TaskPolicy
    has already authorized the individual project capability. Approval
    must evaluate with exactly this profile, never a hardcoded duplicate.
    """
    return "coding_writer" if str(permission_profile or "") == "research" else str(
        permission_profile or "coding_writer")


def _tool_result(call: ToolCall, outcome: ToolOutcome | _Outcome) -> ToolResult:
    audit = dict(outcome.audit)
    # Tool implementations cannot classify a request as a runtime guard refusal.
    audit.pop("execution_disposition", None)
    if call.name == "edit":
        audit["changed"] = outcome.changed
    return ToolResult(
        ok=outcome.ok is True, call=call, model_text=str(outcome.model_text or ""),
        truncated=outcome.truncated,
        presentation=dict(outcome.presentation),
        audit=audit,
        canonical=dict(outcome.canonical),
    )


@dataclass(frozen=True)
class ProjectPolicyCheck:
    denied: bool
    message: str = ""
    approval_required: bool = False
    reason_code: str = ""


class ExecutionDelegate:
    """Real guards + real tools behind the kernel's policy snapshot."""

    def __init__(
        self,
        *,
        session: TaskSession | None = None,
        project_path: Path | str | None = None,
        tool_fns: AgentToolFns | None = None,
        research_tools: ResearchTools | None = None,
        browser_session: BrowserSession | None = None,
        permission_profile: str = "coding_writer",
        approval_available: bool = False,
        change_tracker: ChangeTracker | None = None,
        managed_outputs: ManagedOutputStore | None = None,
        session_id: str = "",
        run_id: str = "",
        frozen_specs: Mapping[str, ToolSpec] | None = None,
        custom_executors: Mapping[str, ToolExecutor | None] | None = None,
        research_scope: AnswerScope | None = None,
    ) -> None:
        self.session = session
        self.project_path: Path | None = Path(str(project_path)).expanduser() if project_path else None
        if self.project_path is not None and not self.project_path.is_dir():
            self.project_path = None
        self.tool_fns: AgentToolFns | None = tool_fns
        self.research_tools = research_tools
        self.browser_session = browser_session or (research_tools.browser_session if research_tools is not None else None)
        self.permission_profile = (permission_profile or "coding_writer")
        self.approval_available = approval_available
        self.change_tracker = change_tracker
        self.managed_outputs = managed_outputs
        self.session_id = session_id
        self.run_id = run_id
        # 本轮冻结绑定：非 None 时解析/执行不再查询实时注册表。
        self._frozen_specs = frozen_specs
        self._custom_executors = custom_executors
        self._research_scope = research_scope
        if self.tool_fns is None and self.project_path is not None:
            from codey.agents.tools import build_project_tool_fns

            self.tool_fns = build_project_tool_fns(
                state_home=managed_outputs.root.parent if managed_outputs is not None else None,
            )

    def _research_resources(self) -> ResearchTools:
        if self.research_tools is None:
            raise ValueError("research tools unavailable")
        return self.research_tools

    def _resolve_spec(self, name: str) -> ToolSpec | None:
        lowered = (name or "").strip().lower()
        if self._frozen_specs is not None:
            return self._frozen_specs.get(lowered)
        try:
            from codey.toolchain.tool_spec import spec_for_tool
        except Exception:
            return None
        try:
            return spec_for_tool(lowered)
        except Exception:
            return None

    def _resolve_custom_executor(self, name: str) -> Callable[[ToolCall], object] | None:
        lowered = (name or "").strip().lower()
        if self._custom_executors is not None:
            return self._custom_executors.get(lowered)
        try:
            from codey.toolchain.tool_spec import custom_executor_for
        except Exception:
            return None
        try:
            return cast("Callable[[ToolCall], object] | None", custom_executor_for(lowered))
        except Exception:
            return None

    def handles(self, name: str) -> bool:
        spec = self._resolve_spec(name)
        if spec is None:
            return False
        if spec.executor == "receipt":
            return bool(self.session is not None)
        if spec.executor == "project":
            return self.project_path is not None and self.tool_fns is not None
        if spec.executor in {"source", "knowledge"}:
            return bool(self.research_tools is not None)
        if spec.executor == "browser":
            # Route unavailable resources to a deterministic production error;
            # never fall through to a custom executor for a built-in tool.
            return True
        try:
            if self._resolve_custom_executor(name or "") is not None:
                return True
        except Exception:
            pass
        return False

    def execute(self, call: ToolCall, *, turn: int = 0,
                tool_index: int = 0) -> tuple[ToolResult, bool, int | None]:
        name = (call.name or "").strip().lower()
        try:
            spec = self._resolve_spec(name)
            executor = spec.executor if spec is not None else ""
        except Exception:
            executor = ""
        if executor == "receipt":
            from codey.operations.tool_result_reader import read_tool_result

            result = read_tool_result(cast("ReceiptReaderContext", self), call)
            return result, result.ok, None
        if executor == "project":
            return self._execute_project(call)
        if executor == "browser":
            return self._execute_browser(call, turn=turn, tool_index=tool_index)
        if executor in {"source", "knowledge"}:
            if not self._reserve_research_acquisition(name, turn, tool_index):
                return ToolResult(ok=False, call=call, model_text="ERROR: research acquisition budget exhausted"), False, None
            return self._execute_research(call, turn=turn, tool_index=tool_index)
        # Third-task tools run via the frozen turn binding when present,
        # otherwise the live registry.
        try:
            fn = self._resolve_custom_executor(name)
        except Exception:
            fn = None
        if callable(fn):
            try:
                produced = fn(call)
            except Exception as exc:
                result = ToolResult(ok=False, call=call, model_text=f"ERROR: {exc or 'tool failed'}")
                return result, False, None
            if not isinstance(produced, ToolResult):
                result = ToolResult(ok=False, call=call, model_text="ERROR: executor must return a structured ToolResult")
                return result, False, None
            return produced, produced.ok, None
        result = ToolResult(ok=False, call=call, model_text=f"ERROR: no production executor for {name or '?'}")
        return result, False, None

    def _execute_browser(self, call: ToolCall, *, turn: int,
                         tool_index: int) -> tuple[ToolResult, bool, int | None]:
        check = self._policy_check(call)
        if check.denied:
            from codey.operations.kernel_result import policy_denial_result

            return policy_denial_result(call, check.message, reason_code=check.reason_code), False, None
        browser = self.browser_session
        if browser is None:
            return ToolResult(ok=False, call=call, model_text="ERROR: browser session unavailable"), False, None
        try:
            assert self.session is not None
            with self.session.projection_lock:
                browser.prepare_from_receipts(tuple(self.session._memory_results.values()))
            checks = self.session.policy.required_checks
            trace_document = (call.name in browser_receipt_tools(checks) or
                              (call.name == "browser_eval" and has_browser_field_bindings(checks)))
            document = browser.document_identity() if trace_document else None
            result, ok, code = self._execute_browser_operation(browser, call, turn=turn, tool_index=tool_index)
            if ok and document is not None and browser.document_identity() == document:
                result = replace(result, audit={**result.audit, "browser_document": document})
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

    def _execute_browser_operation(self, browser: BrowserSession, call: ToolCall, *, turn: int,
                                   tool_index: int) -> tuple[ToolResult, bool, int | None]:
        try:
            if call.name == "browser_read":
                if not self._reserve_research_acquisition(call.name, turn, tool_index):
                    return ToolResult(ok=False, call=call,
                                      model_text="ERROR: research acquisition budget exhausted"), False, None
                from codey.research.source_reading import render_source_read

                tools = self._observation_tools(source_read=True)
                page = browser.capture()
                url = str(page.get("url") or "")
                outcome = tools.gateway.capture(url, page)
                opened = render_source_read(
                    outcome, tools.ledger,
                    offset=require_json_integer(call.args.get("offset", 0), "offset"),
                    limit=require_json_integer(call.args.get("limit", 6000), "limit"),
                    coherent=tools.coherent_passages,
                )
                return self._opened_result(call, opened, url, ledger=tools.ledger, turn=turn, tool_index=tool_index)
            output = self._browser_action(browser, call)
            value = {**output.observation, "text": output.observation.get("text", output.model_text)}
            result = ToolResult(ok=output.ok, call=call, model_text=output.model_text,
                                canonical={"value": value})
            return result, result.ok, None
        except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
            raise
        except Exception as exc:
            return ToolResult(ok=False, call=call, model_text=f"ERROR: {exc}"), False, None

    def _browser_action(self, browser: BrowserSession, call: ToolCall) -> BrowserToolOutput:
        args = call.args
        ref = str(args.get("ref") or "")
        if call.name == "browser_snapshot":
            return browser.snapshot(max_elements=require_json_integer(args.get("max_elements", 80), "max_elements"),
                                    max_text=require_json_integer(args.get("max_text", 12000), "max_text"))
        if call.name == "browser_click":
            return browser.click(ref)
        if call.name == "browser_fill":
            return browser.fill(ref, str(args.get("value") or ""))
        if call.name == "browser_scroll":
            return browser.scroll(str(args.get("direction") or ""), require_json_integer(args.get("amount"), "amount"))
        if call.name == "browser_wait":
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
            from codey.storage.input_attachments import InputAttachmentStore

            if self.managed_outputs is None or not self.session_id:
                raise ValueError("task-scoped attachment storage unavailable")
            identity = str(args.get("attachment_id") or "")
            file = InputAttachmentStore(self.managed_outputs.root.parent, self.session_id).read_file(identity)
            output = browser.upload(ref, {"name": file.name, "mimeType": file.media_type, "buffer": file.data})
            return replace(output, observation={**output.observation, "attachment_id": identity})
        if call.name == "browser_download":
            return self._browser_download(browser, ref)
        raise ValueError(f"unsupported browser tool {call.name}")

    def _browser_download(self, browser: BrowserSession, ref: str) -> BrowserToolOutput:
        if self.managed_outputs is None or not self.session_id or not self.run_id:
            raise ValueError("task-scoped download storage unavailable")
        output = browser.download(ref)
        if not output.ok:
            return output
        try:
            stored = self.managed_outputs.write_file_artifact(
                session_id=self.session_id, run_id=self.run_id,
                source=Path(str(output.observation["path"])), permission_profile=self.permission_profile)
        except (OSError, ValueError) as exc:
            return replace(output, ok=False, model_text=f"ERROR: download delivered but persistence failed: {exc}",
                           observation={"action": "download", "delivery": "delivered", "storage": "failed"})
        return replace(output, model_text=f"Download retained: {output.observation['suggested_filename']} "
                       f"({stored.stored_bytes} bytes, SHA256 {stored.sha256})",
                       observation={**output.observation, "path": str(stored.path), "handle": stored.handle,
                                    "bytes": stored.stored_bytes, "sha256": stored.sha256})

    def _policy_check(self, call: ToolCall) -> ProjectPolicyCheck:
        """Keep machine-readable denial separate from its explanation."""

        spec = self._resolve_spec(call.name)
        if spec is None:
            return ProjectPolicyCheck(True, "tool definition unavailable", False)
        if self.session is None:
            return ProjectPolicyCheck(True, "task session unavailable", False)
        if not self.session.policy.allows(spec.grant):
            return ProjectPolicyCheck(True, f"task policy denied {call.name}", False)
        if spec.executor != "project":
            return ProjectPolicyCheck(False, "", False)
        if self.project_path is None:
            return ProjectPolicyCheck(True, "no associated project", False)
        try:
            from codey.agents.tool_execution import (
                evaluate_tool_call_policy_for,
                policy_asks_user,
                policy_denied,
            )
        except Exception as exc:
            return ProjectPolicyCheck(True, f"policy unavailable: {exc}", False)
        try:
            runtime_name = {"list_dir": "ls", "read_file": "read", "grep": "search",
                            "find_references": "references"}.get(call.name, call.name)
            decision, _replay = evaluate_tool_call_policy_for(
                ToolCall(runtime_name, dict(call.args or {}), call_id=call.call_id),
                project=self.project_path,
                permission_profile=self._project_permission_profile(),
                approval_available=self.approval_available,
                phase="writer",
                shell=self.session.shell,
            )
        except Exception as exc:
            return ProjectPolicyCheck(True, f"policy error: {exc}", False)
        if decision is not None and policy_denied(decision):
            reason = str(getattr(decision, "reason", "") or getattr(decision, "reason_code", "") or "denied")
            display = decision.display
            if reason == "command_not_allowed" and not self.session.policy.allows("shell.approval"):
                display = ("This task mode cannot request shell approval. Use an interactive coding task "
                           "with command approvals enabled, or use an allowed verification command.")
            elif reason == "command_not_allowed":
                display += " Submit this command on its own in an interactive task to request approval."
            return ProjectPolicyCheck(True, f"project guard denied {call.name}: {reason}; {display}", False, reason)
        try:
            if (call.name or "") == "shell" and policy_asks_user(decision):
                return ProjectPolicyCheck(True, "shell requires user approval", True)
        except Exception:
            pass
        return ProjectPolicyCheck(False, "", False)


    def _project_permission_profile(self) -> str:
        return effective_project_profile(self.permission_profile)

    def _execute_project(self, call: ToolCall) -> tuple[ToolResult, bool, int | None]:
        if self.project_path is None:
            return ToolResult(ok=False, call=call, model_text="ERROR: project path unavailable"), False, None
        if self.tool_fns is None:
            return ToolResult(ok=False, call=call, model_text="ERROR: project tools unavailable"), False, None
        project_path = self.project_path
        check = self._policy_check(call)
        if check.denied:
            from codey.operations.kernel_result import policy_denial_result

            result = policy_denial_result(call, check.message, reason_code=check.reason_code)
            return result, False, None
        name = (call.name or "").strip().lower()
        runtime_name = {"list_dir": "ls", "read_file": "read", "grep": "search",
                        "find_references": "references"}.get(name, name)
        try:
            if runtime_name in {"ls", "read", "search", "references"}:
                from codey.agents.tool_execution import execute_information_tool_call

                outcome = execute_information_tool_call(
                    project_path, self.tool_fns,
                    ToolCall(runtime_name, dict(call.args or {})),
                )
                result = _tool_result(call, outcome)
                ok = outcome.ok if type(getattr(outcome, "ok", None)) is bool else False
                return result, ok, None
            if name == "edit":
                outcome = self._execute_edit(call)
                result = _tool_result(call, outcome)
                ok = outcome.ok if type(getattr(outcome, "ok", None)) is bool else False
                return result, ok, None
            if name == "run":
                from dataclasses import replace

                from codey.toolchain.command_output import output_tail, tail_lines

                tail = tail_lines((call.args or {}).get("output_tail_lines"))
                outcome = self.tool_fns.execute_run_command(
                    project_path, str((call.args or {}).get("path", ".") or "."),
                    str((call.args or {}).get("command", "") or ""),
                    permission_profile=self._project_permission_profile(), phase="writer",
                    tool_id=str(getattr(call, "call_id", "") or ""),
                )
                if tail is not None:
                    outcome = replace(outcome, model_text=f"Exit code: {outcome.exit_code}\n" + output_tail(outcome.model_text, tail),
                                      presentation={**outcome.presentation, "output_tail_lines": tail})
                result = _tool_result(call, outcome)
                ok = outcome.ok if type(getattr(outcome, "ok", None)) is bool else False
                return result, ok, getattr(outcome, "exit_code", None)
        except Exception as exc:
            result = ToolResult(ok=False, call=call, model_text=f"ERROR: {exc}")
            return result, False, None
        result = ToolResult(ok=False, call=call, model_text=f"ERROR: unsupported project tool {name}")
        return result, False, None

    def _execute_edit(self, call: ToolCall) -> ToolOutcome:
        from codey.agents.protocol import canonical_project_path
        from codey.agents.tool_execution import read_before_edit_outcome
        from codey.toolchain.runtime import EditBlock, ToolOutcome, safe_join

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
        if "content" in args:
            from codey.operations.edit_base import content_edit_version

            version = content_edit_version(self.session, project_path, canonical, args)
            if self.change_tracker is not None:
                self.change_tracker.capture_before(path)
            content = str(args.get("content") or "")
            if version is None:
                outcome = self.tool_fns.write_file(project_path, path, content)
            else:
                outcome = self.tool_fns.write_file(project_path, path, content, expected_version=version)
            if outcome.ok and outcome.changed and self.change_tracker is not None:
                self.change_tracker.capture_after(path)
            return outcome
        guard = read_before_edit_outcome(
            project_path, path, set(getattr(self.session, "read_files", set()) or set()),
        )
        if guard is not None:
            return guard
        # Canonical protocol only: replacements with old_string/new_string.
        # Top-level aliases are rejected before any file write.
        for legacy in ("search", "replace"):
            if legacy in args:
                return ToolOutcome.error(f"edit rejects legacy alias: {legacy}")
        replacements = args.get("replacements")
        if not isinstance(replacements, list) or not replacements:
            return ToolOutcome.error("edit needs content or exact replacements")
        blocks: list[dict[str, str]] = []
        for item in replacements:
            if not isinstance(item, dict):
                return ToolOutcome.error("edit replacement must be a mapping")
            if set(item) != {"old_string", "new_string"}:
                return ToolOutcome.error("edit replacement must be exactly old_string/new_string")
            search = str(item.get("old_string") or "")
            replace = str(item.get("new_string") or "")
            if not search:
                return ToolOutcome.error("edit replacement search must be non-empty")
            blocks.append({"old_string": search, "new_string": replace})
        if not blocks:
            return ToolOutcome.error("edit needs content or exact replacements")
        edit_blocks = [EditBlock(old_string=b["old_string"], new_string=b["new_string"]) for b in blocks]
        if self.change_tracker is not None:
            self.change_tracker.capture_before(path)
        outcome = self.tool_fns.edit_file(self.project_path, path, edit_blocks)
        if outcome.ok and outcome.changed and self.change_tracker is not None:
            self.change_tracker.capture_after(path)
        return outcome

    def _opened_result(self, call: ToolCall, opened: ResearchToolOutput, url: str, *, ledger: ResearchLedger, turn: int,
                       tool_index: int) -> tuple[ToolResult, bool, int | None]:
        model_text = opened.model_text
        if not opened.ok:
            canonical: JsonObject = {"source_failure": {"url": url, "detail": model_text, "code": opened.failure_code}}
            if opened.acquisition:
                canonical["source_acquisition"] = opened.acquisition
            return ToolResult(ok=False, call=call, model_text=model_text,
                              canonical=canonical), False, None
        if not isinstance(ledger, ResearchLedger):
            raise ValueError("successful source read requires a research ledger observation")
        from codey.research.answer_contract import AnswerScope

        if isinstance(self._research_scope, AnswerScope):
            from codey.research.answer_context import fitting_full_read

            resources = self._research_resources()
            assert self.session is not None
            opened = fitting_full_read(self.session, resources.ledger,
                                       call, opened, provider=resources.context_provider)
            model_text = opened.model_text
        observation = opened.read_observation or ledger_observation(ledger, "open", url=url)
        from codey.research.output_receipts import maybe_externalize_output

        title = next((line.removeprefix("Title: ") for line in model_text.splitlines()
                      if line.startswith("Title: ")), "")
        outcome = maybe_externalize_output(
            store=self.managed_outputs, session_id=self.session_id, run_id=self.run_id,
            permission_profile=self.permission_profile, call=call,
            output=opened.receipt_text or model_text, ok=opened.ok,
            turn=turn, tool_index=tool_index, presentation_result=title,
            model_text_override=model_text,
        )
        base = _tool_result(call, outcome)
        merged = dict(base.canonical)
        merged["research_observation"] = observation
        if opened.acquisition:
            merged["source_acquisition"] = opened.acquisition
        if opened.read_passages:
            merged["read_passages"] = list(opened.read_passages)
        result = ToolResult(
            ok=base.ok, call=base.call, model_text=base.model_text, truncated=base.truncated,
            presentation=dict(base.presentation) if isinstance(base.presentation, dict) else {},
            audit=dict(base.audit) if isinstance(base.audit, dict) else {},
            canonical=merged,
        )
        return result, True, None

    def _observation_tools(self, *, source_read: bool = False) -> ResearchTools:
        # Acquisition mutates a temporary ledger. Only the committed result
        # receipt may publish it into the run's formal projection.
        tools = copy.copy(self._research_resources())
        from codey.research.answer_contract import AnswerScope

        tools.coherent_passages = isinstance(self._research_scope, AnswerScope)
        # Per-call presentation follows the admitted request, including replay.
        tools.evidence_only = self.session is not None and self.session.task_kind == "evidence_followup"
        tools.finding_report = (self.session is not None and self.session.task_kind == "research"
                                and self.session.policy.strict_research)
        tools.answer_submission = self._research_scope is not None
        ledger = tools.ledger
        # Acquisition keeps source version metadata for monotonic timestamps,
        # while the receipt still starts without stale bodies or passages.
        with self.session.projection_lock if self.session is not None else contextlib.nullcontext():
            tools.ledger = ledger.clone_for_acquisition() if source_read else ledger.clone()
        tools.sources_read = {url for source in ledger.opened_sources
                              for url in (source.requested_url, source.final_url)}
        tools.search_result_urls = {hit.url for search in ledger.searches for hit in search.results}
        return tools

    def _reserve_research_acquisition(self, name: str, turn: int, tool_index: int) -> bool:
        if self.session is not None and self.session.research_acquisition_limits is not None:
            category = "search" if name == "web_search" else "open" if is_source_read_tool(name) else ""
            if category:
                with self.session.projection_lock:
                    slots = self.session.research_acquisition_slots
                    limit = self.session.research_acquisition_limits[0 if category == "search" else 1]
                    # Rebuilt from receipts on restore, plus in-flight slots.
                    # Failed acquisitions consume budget too.
                    previous = {key for key, kind in slots.items() if kind == category}
                    if (turn, tool_index) not in previous and len(previous) >= limit:
                        return False
                    slots[(turn, tool_index)] = category
        return True

    def _research_call_denial(self, call: ToolCall) -> tuple[ToolResult, bool, int | None] | None:
        from codey.operations.kernel_result import policy_denial_result
        from codey.research.answer_contract import digest, whole_research

        try:
            if (call.name == "knowledge_commit" and self.session is not None
                    and self.session.task_kind == "research"):
                raise ValueError("Research does not modify existing knowledge objects; use knowledge_updates in the complete answer")
            if self.session is not None and whole_research(self.session) and call.name in {
                "research_plan", "knowledge_write", "knowledge_link", "save_finding", "save_inference",
            }:
                raise ValueError("Research uses one complete answer; save new candidates with knowledge_updates in submit_research_answer")
            if self._research_scope is not None:
                scope = self._research_scope
                if self.session is None or scope.task_digest != digest(self.session.task_text):
                    raise ValueError("research scope belongs to another task")
        except ValueError as exc:
            return policy_denial_result(call, str(exc), reason_code="research_scope_denied"), False, None
        return None

    def _execute_research(self, call: ToolCall, *, turn: int = 0,
                          tool_index: int = 0) -> tuple[ToolResult, bool, int | None]:
        name = (call.name or "").strip().lower()
        tools = self._observation_tools(source_read=name in SOURCE_READ_TOOL_NAMES)
        args = dict(call.args or {})
        try:
            denied = self._research_call_denial(call)
            if denied is not None:
                return denied
            if name == "research_plan":
                return self._record_research_plan(call, tools)
            if name == "submit_research_answer":
                return self._submit_research_answer(call, tools)
            if name == "web_search":
                from codey.research.text_args import first_text_arg

                outcome = tools.web_search(first_text_arg(args, "query"))
                canonical = {}
                ledger = getattr(tools, "ledger", None)
                if outcome.ok:
                    if not isinstance(ledger, ResearchLedger):
                        raise ValueError("successful search requires a research ledger observation")
                    canonical["research_observation"] = ledger_observation(ledger, name)
                return ToolResult(ok=outcome.ok, call=call, model_text=outcome.model_text, canonical=canonical), outcome.ok, None
            if name in SOURCE_READ_TOOL_NAMES:
                return self._execute_source_open(call, tools, turn=turn, tool_index=tool_index)
            if name == "source_search":
                query = str(args.get("query") or "")
                url = str(args.get("url") or "")
                if not url and str(args.get("source_id") or ""):
                    url = self._resolve_alias_url("reopen_source", {"source_id": args.get("source_id")}) or ""
                outcome = tools.source_search(url, query, args.get("limit", 6))
                if outcome.ok:
                    ledger = getattr(tools, "ledger", None)
                    if not isinstance(ledger, ResearchLedger):
                        raise ValueError("successful source search requires a research ledger observation")
                    observation = ledger_observation(ledger, name)
                    text, mapping = self._build_hit_mapping(source_search_observation(observation))
                    hit_canonical: dict[str, object] = {"hit_targets": mapping, "research_observation": observation}
                    return (
                        ToolResult(ok=outcome.ok, call=call, model_text=text, canonical=hit_canonical),
                        True, None,
                    )
                return ToolResult(ok=outcome.ok, call=call, model_text=outcome.model_text), outcome.ok, None
            if name == "knowledge_search":
                from codey.research.text_args import first_text_arg

                outcome = tools.knowledge_search(first_text_arg(args, "query"),
                                                 depth=require_json_integer(args.get("depth", 1), "depth"))
                search_canonical = ({"knowledge_search_empty": True}
                    if outcome.ok and getattr(outcome, "knowledge_search_empty", False) is True else {})
                return ToolResult(ok=outcome.ok, call=call, model_text=outcome.model_text,
                                  canonical=search_canonical), outcome.ok, None
            if name == "knowledge_read":
                return self._read_research_knowledge(call, tools)
            if name == "knowledge_commit":
                return self._commit_research_knowledge(call, tools)
            if name in {"knowledge_write", "save_finding", "save_inference"}:
                return self._execute_research_write(call, tools)
            if name == "knowledge_link":
                links_before = tools.links_created
                outcome = tools.knowledge_link(str(args.get("src") or ""), str(args.get("dst") or ""),
                                            str(args.get("kind") or "relates"))
                return ToolResult(ok=outcome.ok, call=call, model_text=outcome.model_text,
                    canonical={"knowledge_links_created": tools.links_created - links_before}), outcome.ok, None
        except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
            raise
        except Exception as exc:
            return ToolResult(ok=False, call=call, model_text=f"ERROR: {exc}"), False, None
        return ToolResult(ok=False, call=call, model_text=f"ERROR: unknown research tool {name}"), False, None

    def _execute_source_open(self, call: ToolCall, tools: ResearchTools, *, turn: int,
                             tool_index: int) -> tuple[ToolResult, bool, int | None]:
        from codey.operations.source_execution import open_source
        from codey.research.source_snapshots import SourceSnapshotUnavailable

        if call.name == "open_url":
            url = str(call.args.get("url") or "")
        else:
            url = self._resolve_alias_url(call.name, dict(call.args))
            if not url:
                field = _CONTROLLER_ALIAS_ID_ARG[call.name][1]
                detail = f"ERROR: unknown {call.name} id. Use {field} from the observed references. "
                detail += "A tool result_ref or out_ artifact handle is not a source locator."
                return ToolResult(ok=False, call=call, model_text=detail), False, None
        target = (self.session.hit_targets.get(str(call.args.get("hit_id") or "").lower())
                  if call.name == "open_hit" and self.session is not None else None)
        try:
            opened = open_source(call, tools, self._research_resources().ledger, url, target)
        except SourceSnapshotUnavailable as exc:
            opened = ResearchToolOutput(f"ERROR: {exc}", ok=False, failure_code="snapshot_unavailable")
        return self._opened_result(call, opened, url, ledger=tools.ledger, turn=turn, tool_index=tool_index)

    def _commit_research_knowledge(self, call: ToolCall, tools: ResearchTools) -> tuple[ToolResult, bool, int | None]:
        outcome = tools.knowledge_commit(call.args)
        return ToolResult(ok=outcome.ok, call=call, model_text=outcome.model_text), outcome.ok, None

    def _read_research_knowledge(self, call: ToolCall, tools: ResearchTools) -> tuple[ToolResult, bool, int | None]:
        from codey.research.answer_contract import AnswerScope
        from codey.research.questions import question_ref

        args = call.args
        identity = str(args.get("id") or "")
        revision = require_json_integer(args["revision"], "revision") if args.get("revision") is not None else None
        outcome = tools.knowledge_read(identity, revision=revision, question_ref=question_ref(args, tools.ledger),
                                       offset=require_json_integer(args.get("offset", 0), "offset"),
                                       limit=require_json_integer(args.get("limit", 6000), "limit"),
                                       whole_answer=isinstance(self._research_scope, AnswerScope))
        text = outcome.model_text
        if not outcome.ok and isinstance(self._research_scope, AnswerScope):
            if identity in tools.ledger.read_passages:
                text = ("ERROR: this id names a task-read passage, not stored knowledge. "
                        "Cite its existing [eN] label directly. knowledge_read needs an exact stored id and revision.")
            elif identity in getattr(self.session, "source_ids", {}):
                text = ("ERROR: this id is a source locator, not stored knowledge. "
                        "Use the existing [eN] evidence, or the displayed source-reading action for unread text. "
                        "knowledge_read needs an exact stored id and revision.")
        canonical: dict[str, object] = {
            "research_reference_bindings": {k: v.to_dict() for k, v in tools.ledger.references.items()}} if outcome.ok else {}
        if outcome.ok and outcome.read_passages:
            canonical.update(research_observation=outcome.read_observation, read_passages=list(outcome.read_passages))
        return ToolResult(ok=outcome.ok, call=call, model_text=text, canonical=canonical), outcome.ok, None

    def _record_research_plan(self, call: ToolCall, tools: ResearchTools) -> tuple[ToolResult, bool, int | None]:
        outcome = tools.research_plan(call.args.get("questions"))
        canonical = {"research_observation": outcome.read_observation} if outcome.ok else {}
        return ToolResult(ok=outcome.ok, call=call, model_text=outcome.model_text, canonical=canonical), outcome.ok, None

    def _submit_research_answer(self, call: ToolCall, tools: ResearchTools) -> tuple[ToolResult, bool, int | None]:
        from codey.research.answer_contract import AnswerScope, submit_answer

        if not isinstance(self._research_scope, AnswerScope):
            raise ValueError("submit_research_answer requires an original frozen whole-answer scope")
        declared = submit_answer(self._research_scope, call.args)
        return ToolResult(call, "Whole answer recorded. The application will validate and publish this submission.",
                          ok=True, canonical={"research_submission": declared}), True, None

    def _execute_research_write(self, call: ToolCall, tools: ResearchTools) -> tuple[ToolResult, bool, int | None]:
        name, args = call.name, dict(call.args or {})
        before = len(getattr(getattr(tools, "ledger", None), "evidence_items", ()) or ())
        created_before, updated_before = set(tools.created_ids), set(tools.updated_ids)
        links_before = tools.links_created
        lowered, problem = (self._lower_finding_source(args) if name == "save_finding" else
                            (args, "") if name == "save_inference" else self._lower_knowledge_sources(args))
        if problem:
            return ToolResult(ok=False, call=call, model_text=f"ERROR: {problem}"), False, None
        outcome = (tools.save_finding(lowered) if name == "save_finding" else
                   tools.save_inference(lowered) if name == "save_inference" else tools.knowledge_write(lowered))
        if not outcome.ok:
            return ToolResult(ok=outcome.ok, call=call, model_text=outcome.model_text), False, None
        evidence = self._ledger_evidence(before, tools.ledger)
        evidence_canonical: dict[str, object] = {"evidence_items": evidence}
        if name in {"save_finding", "save_inference"}:
            evidence_canonical["research_reference_bindings"] = {k: v.to_dict() for k, v in tools.ledger.references.items()}
        evidence_canonical["knowledge_links_created"] = tools.links_created - links_before
        proposals: list[object] = []
        created = [item for item in tools.created_ids if item not in created_before]
        target = str(lowered.get("id") or "")
        updated = list(dict.fromkeys([item for item in tools.updated_ids if item not in updated_before] +
            ([target] if target in tools.updated_ids else [])))
        evidence_canonical["knowledge_notes"] = {
            "created": created,
            "updated": updated,
            "proposals": proposals,
        }
        affected = list(dict.fromkeys([*created, *updated]))
        self._staged_note_receipt(tools, affected, proposals, evidence_canonical)
        ledger = getattr(tools, "ledger", None)
        if isinstance(ledger, ResearchLedger) and name != "save_inference":
            evidence_canonical["research_observation"] = ledger_observation(ledger, name, evidence_start=before)
        return (
            ToolResult(ok=outcome.ok, call=call, model_text=outcome.model_text, canonical=evidence_canonical),
            True, None,
        )

    @staticmethod
    def _staged_note_receipt(tools: ResearchTools, created: list[str], proposals: list[object], canonical: dict[str, object]) -> None:
        from codey.providers.response_record import json_value
        from codey.research.staging import StagedKnowledgeStore

        store = tools.store
        if isinstance(store, StagedKnowledgeStore):
            proposals.extend(json_value(store._staged_notes[identity]) for identity in created)
            canonical["knowledge_bases"] = {identity: store._staged_bases[identity] for identity in created}
            canonical["knowledge_batches"] = {
                identity: {"commit_id": batch[0], "mutations": [
                    {"value": mutation.value.to_dict(), "expected_revision": mutation.expected_revision}
                    for mutation in batch[1]]}
                for identity, batch in store._staged_knowledge.items() if identity in created
            }

    def _lower_knowledge_sources(self, args: ToolArgs) -> tuple[ToolArgs, str]:
        import re

        sources = dict(getattr(self.session, "source_ids", {}) or {})

        def lower(value: object) -> tuple[str, str]:
            text = str(value or "").strip()
            key = text.lower()
            if key in sources:
                return str(sources[key]), ""
            if re.fullmatch(r"s[1-9][0-9]*", key):
                return "", f"unknown source_id: {text}"
            return text, ""

        lowered = dict(args)
        raw_sources = args.get("sources")
        if isinstance(raw_sources, list):
            rows: list[JsonValue] = []
            for value in raw_sources:
                resolved, error = lower(value)
                if error:
                    return {}, error
                rows.append(resolved)
            lowered["sources"] = rows
        raw_evidence = args.get("evidence")
        if isinstance(raw_evidence, list):
            evidence: list[JsonValue] = []
            for value in raw_evidence:
                if not isinstance(value, dict):
                    evidence.append(value)
                    continue
                row = dict(value)
                for key in ("source_url", "source"):
                    if key in row:
                        resolved, error = lower(row[key])
                        if error:
                            return {}, error
                        row[key] = resolved
                evidence.append(row)
            lowered["evidence"] = evidence
        return lowered, ""

    def _lower_finding_source(self, args: ToolArgs) -> tuple[ToolArgs, str]:
        # The selected passage owns the source identity. An explicitly supplied
        # source is only an assertion to validate, including frozen older calls.
        if "source_ref" not in args:
            return args, ""
        reference = args.get("source_ref")
        sources = dict(getattr(self.session, "source_ids", {}) or {})
        if not isinstance(reference, str) or reference not in sources:
            return {}, "source_ref must be an exact opened source reference: " + ", ".join(sources)
        return {**args, "source_ref": sources[reference]}, ""

    def _resolve_alias_url(self, alias: str, args: ToolArgs) -> str:
        from codey.operations.source_execution import resolve_alias_url

        return resolve_alias_url(self.session, alias, args)

    def _build_hit_mapping(self, observation: SourceSearchObservation) -> tuple[str, dict[str, HitTarget]]:
        from codey.research.source_locators import build_hit_mapping

        return build_hit_mapping(observation, self.session.hit_targets if self.session is not None else {})

    def _ledger_evidence(self, before: int, ledger: ResearchLedger) -> list[dict[str, str]]:
        items = list(getattr(ledger, "evidence_items", ()) or [])
        fresh = items[before:] if before >= 0 else items
        evidence: list[dict[str, str]] = []
        for item in fresh:
            url = str(getattr(item, "source_url", "") or "").strip()
            excerpt = str(getattr(item, "excerpt", "") or "").strip()
            if url and excerpt:
                evidence.append({"source_url": url, "excerpt": excerpt[:600]})
        return evidence


__all__ = ["ExecutionDelegate", "build_research_tools", "close_research_tools"]
