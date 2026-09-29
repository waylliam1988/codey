"""Production tool execution for the unified kernel (operations layer).

Project tools reuse the coding guards and readers (path safety, command
policy, shell approval surface); Research tools reuse ResearchTools, the
source gateway, and the evidence ledger. Completion facts come only from
successful tool results and ledger
state, never from substring matching or model-supplied parameters.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from codey.runtime.core.models import ToolCall, ToolResult


def build_research_tools(deps: Any, *, session_id: str, project: str) -> Any | None:
    """Research execution adapter: real search + store + ledger tools.

    Moved here from the old task entry so the entry stays orchestration-only;
    executors live with execution. Returns None when Research is unconfigured.
    """
    knowledge_store = getattr(deps, "knowledge_store", None)
    if knowledge_store is None:
        return None
    search_factory = getattr(deps, "search_factory", None)
    if not callable(search_factory):
        try:
            from codey.operations.research_flow import default_research_search_provider
        except Exception:
            return None
        search_factory = default_research_search_provider
    try:
        search = search_factory()
    except Exception:
        return None
    try:
        from codey.knowledge.changes import KnowledgeChanges

        changes = KnowledgeChanges(root=getattr(knowledge_store, "root", "."))
    except Exception:
        return None
    try:
        from codey.research.tools import ResearchTools

        return ResearchTools(
            search=search,
            store=knowledge_store,
            changes=changes,
            diagnostics=None,
            session_id=session_id,
            project=project,
        )
    except Exception:
        return None


def effective_project_profile(permission_profile: object) -> str:
    """Project-guard profile shared by execution and shell approval.

    The Research profile controls research context and source tools. A
    project tool still needs the coding path/command guard; TaskPolicy
    has already authorized the individual project capability. Approval
    must evaluate with exactly this profile, never a hardcoded duplicate.
    """
    return "coding_writer" if str(permission_profile or "") == "research" else str(
        permission_profile or "coding_writer")


def _tool_result(call: ToolCall, outcome: Any) -> ToolResult:
    audit = dict(getattr(outcome, "audit", {}) or {})
    if call.name == "edit":
        audit["changed"] = bool(getattr(outcome, "changed", False))
    return ToolResult(
        call=call, model_text=str(outcome.model_text or ""),
        truncated=bool(getattr(outcome, "truncated", False)),
        presentation=dict(getattr(outcome, "presentation", {}) or {}),
        audit=audit,
        canonical=dict(getattr(outcome, "canonical", {}) or {}),
    )


def _is_ok_text(text: str) -> bool:
    lowered = str(text or "")
    return not (lowered.startswith("ERROR:") or lowered.startswith("SKIPPED:")
                or lowered.startswith("NEEDS_OPEN:"))


class ExecutionDelegate:
    """Real guards + real tools behind the kernel's policy snapshot."""

    def __init__(
        self,
        *,
        session: Any = None,
        project_path: Any = None,
        tool_fns: Any = None,
        research_tools: Any = None,
        permission_profile: str = "coding_writer",
        approval_available: bool = False,
        change_tracker: Any = None,
        managed_outputs: Any = None,
        session_id: str = "",
        run_id: str = "",
    ) -> None:
        self.session = session
        self.project_path = Path(str(project_path)).expanduser() if project_path else None
        if self.project_path is not None and not self.project_path.is_dir():
            self.project_path = None
        self.tool_fns = tool_fns
        self.research_tools = research_tools
        self.permission_profile = str(permission_profile or "coding_writer")
        self.approval_available = bool(approval_available)
        self.change_tracker = change_tracker
        self.managed_outputs = managed_outputs
        self.session_id = session_id
        self.run_id = run_id
        if self.tool_fns is None and self.project_path is not None:
            try:
                from codey.agents.tools import DEFAULT_TOOL_FNS
            except Exception:
                DEFAULT_TOOL_FNS = None  # type: ignore[assignment]
            self.tool_fns = DEFAULT_TOOL_FNS

    def handles(self, name: str) -> bool:
        lowered = str(name or "").strip().lower()
        try:
            from codey.toolchain.tool_spec import custom_executor_for, spec_for_tool
        except Exception:
            return False
        try:
            spec = spec_for_tool(lowered)
        except Exception:
            return False
        if spec is None:
            return False
        if spec.executor == "project":
            return self.project_path is not None and self.tool_fns is not None
        if spec.executor in {"source", "knowledge"}:
            return self.research_tools is not None
        try:
            if custom_executor_for(lowered) is not None:
                return True
        except Exception:
            pass
        return False

    def execute(self, call: ToolCall, *, turn: int = 0,
                tool_index: int = 0) -> tuple[ToolResult, bool, str, list[dict[str, str]], int | None]:
        name = str(call.name or "").strip().lower()
        try:
            from codey.toolchain.tool_spec import custom_executor_for, spec_for_tool
            spec = spec_for_tool(name)
            executor = spec.executor if spec is not None else ""
        except Exception:
            executor = ""
            custom_executor_for = None  # type: ignore[assignment]
        if executor == "project":
            return self._execute_project(call)
        if executor in {"source", "knowledge"}:
            return self._execute_research(call, turn=turn, tool_index=tool_index)
        # Third-task tools run via the generic ToolSpec executor registry.
        if custom_executor_for is not None:
            try:
                fn = custom_executor_for(name)
            except Exception:
                fn = None
            if callable(fn):
                try:
                    produced = fn(call)
                except Exception as exc:
                    result = ToolResult(call=call, model_text=f"ERROR: {exc or 'tool failed'}")
                    return result, False, "", [], None
                if isinstance(produced, ToolResult):
                    result = produced
                elif isinstance(produced, str):
                    result = ToolResult(call=call, model_text=produced)
                else:
                    result = ToolResult(call=call, model_text=str(produced))
                from codey.operations.kernel_execution import _result_ok as _ok
                try:
                    ok = bool(_ok(name, result))
                except Exception:
                    ok = not str(result.model_text or "").startswith("ERROR:")
                return result, ok, "", [], None
        result = ToolResult(call=call, model_text=f"ERROR: no production executor for {name or '?'}")
        return result, False, "", [], None

    def _policy_check(self, call: ToolCall) -> tuple[bool, str, bool]:
        """Returns (denied, message, approval_required) via the coding guards."""

        if self.project_path is None:
            return True, "no associated project", False
        try:
            from codey.agents.tool_execution import (
                evaluate_tool_call_policy_for,
                policy_asks_user,
                policy_denied,
            )
        except Exception as exc:
            return True, f"policy unavailable: {exc}", False
        try:
            runtime_name = {"list_dir": "ls", "read_file": "read", "grep": "search",
                            "find_references": "references"}.get(call.name, call.name)
            decision, _replay = evaluate_tool_call_policy_for(
                ToolCall(runtime_name, dict(call.args or {}), call_id=call.call_id),
                project=self.project_path,
                permission_profile=self._project_permission_profile(),
                approval_available=self.approval_available,
                phase="writer",
            )
        except Exception as exc:
            return True, f"policy error: {exc}", False
        if policy_denied(decision):
            reason = str(getattr(decision, "reason", "") or getattr(decision, "reason_code", "") or "denied")
            return True, f"project guard denied {call.name}: {reason}", False
        try:
            if str(call.name or "") == "shell" and bool(policy_asks_user(decision)):
                return True, "shell requires user approval", True
        except Exception:
            pass
        return False, "", False

    def _project_permission_profile(self) -> str:
        return effective_project_profile(self.permission_profile)

    def _execute_project(self, call: ToolCall) -> tuple[ToolResult, bool, str, list[dict[str, str]], int | None]:
        denied, message, _approval = self._policy_check(call)
        if denied:
            result = ToolResult(call=call, model_text=f"ERROR: {message}")
            return result, False, "", [], None
        name = str(call.name or "").strip().lower()
        runtime_name = {"list_dir": "ls", "read_file": "read", "grep": "search",
                        "find_references": "references"}.get(name, name)
        try:
            if runtime_name in {"ls", "read", "search", "references"}:
                from codey.agents.tool_execution import execute_information_tool_call

                outcome = execute_information_tool_call(
                    self.project_path, self.tool_fns,
                    ToolCall(runtime_name, dict(call.args or {})),
                )
                result = _tool_result(call, outcome)
                return result, bool(outcome.ok), "", [], None
            if name == "edit":
                outcome = self._execute_edit(call)
                result = _tool_result(call, outcome)
                return result, bool(outcome.ok), "", [], None
            if name == "run":
                outcome = self.tool_fns.execute_run_command(
                    self.project_path, str((call.args or {}).get("path", ".") or "."),
                    str((call.args or {}).get("command", "") or ""),
                    permission_profile=self._project_permission_profile(), phase="writer",
                    tool_id=str(getattr(call, "call_id", "") or ""),
                )
                result = _tool_result(call, outcome)
                return result, bool(outcome.ok), "", [], getattr(outcome, "exit_code", None)
        except Exception as exc:
            result = ToolResult(call=call, model_text=f"ERROR: {exc}")
            return result, False, "", [], None
        result = ToolResult(call=call, model_text=f"ERROR: unsupported project tool {name}")
        return result, False, "", [], None

    def _execute_edit(self, call: ToolCall) -> Any:
        from codey.agents.protocol import canonical_project_path
        from codey.agents.tool_execution import read_before_edit_outcome
        from codey.toolchain.runtime import ToolOutcome, safe_join

        args = dict(call.args or {})
        path = str(args.get("path", "") or "")
        try:
            canonical = canonical_project_path(self.project_path, path)
            target = safe_join(self.project_path, canonical)
        except ValueError:
            return ToolOutcome.error("workspace_escape: path escapes project root")
        if "content" in args:
            if target.is_file():
                return ToolOutcome.error(
                    f"content is only allowed when creating a new file; use replacements for existing file: {canonical}"
                )
            if self.change_tracker is not None:
                self.change_tracker.capture_before(path)
            outcome = self.tool_fns.write_file(self.project_path, path, str(args.get("content") or ""))
            if outcome.ok and outcome.changed and self.change_tracker is not None:
                self.change_tracker.capture_after(path)
            return outcome
        guard = read_before_edit_outcome(
            self.project_path, path, set(getattr(self.session, "read_files", set()) or set()),
        )
        if guard is not None:
            return guard
        replacements = args.get("replacements")
        blocks: list[dict[str, str]] = []
        if isinstance(replacements, list):
            for item in replacements:
                if isinstance(item, dict) and ("search" in item or "old_string" in item):
                    blocks.append({
                        "old_string": str(item.get("search", item.get("old_string")) or ""),
                        "new_string": str(item.get("replace", item.get("new_string")) or ""),
                    })
        elif args.get("old_string") is not None:
            blocks.append({"old_string": str(args.get("old_string") or ""),
                           "new_string": str(args.get("new_string") or "")})
        if not blocks:
            return ToolOutcome.error("edit needs content or exact replacements")
        try:
            from codey.toolchain.runtime import EditBlock

            edit_blocks = [EditBlock(search=b["old_string"], replace=b["new_string"]) for b in blocks]
        except Exception:
            edit_blocks = blocks  # type: ignore[assignment]
        if self.change_tracker is not None:
            self.change_tracker.capture_before(path)
        outcome = self.tool_fns.edit_file(self.project_path, path, edit_blocks)
        if outcome.ok and outcome.changed and self.change_tracker is not None:
            self.change_tracker.capture_after(path)
        return outcome

    def _opened_result(self, call: ToolCall, opened: Any, url: str, *, turn: int,
                       tool_index: int) -> tuple[ToolResult, bool, str, list[dict[str, str]], int | None]:
        model_text = str(getattr(opened, "model_text", opened) or "")
        if not _is_ok_text(model_text):
            return ToolResult(call=call, model_text=model_text), False, "", [], None
        from codey.research.output_receipts import maybe_externalize_output

        title = next((line.removeprefix("Title: ") for line in model_text.splitlines()
                      if line.startswith("Title: ")), "")
        outcome = maybe_externalize_output(
            store=self.managed_outputs, session_id=self.session_id, run_id=self.run_id,
            permission_profile=self.permission_profile, call=call,
            output=str(getattr(opened, "receipt_text", "") or model_text),
            turn=turn, tool_index=tool_index, presentation_result=title,
            model_text_override=model_text,
        )
        return _tool_result(call, outcome), True, self._ledger_final_url({"url": url}), [], None

    def _execute_research(self, call: ToolCall, *, turn: int = 0,
                          tool_index: int = 0) -> tuple[ToolResult, bool, str, list[dict[str, str]], int | None]:
        tools = self.research_tools
        name = str(call.name or "").strip().lower()
        args = dict(call.args or {})
        try:
            if name == "web_search":
                from codey.research.text_args import first_text_arg

                text = tools.web_search(first_text_arg(args, "query"))
                return ToolResult(call=call, model_text=text), _is_ok_text(text), "", [], None
            if name == "open_url":
                opened = tools.open_url(str(args.get("url") or ""), offset=args.get("offset", 0),
                                        limit=args.get("limit", 6000), pages=str(args.get("pages") or ""))
                return self._opened_result(call, opened, str(args.get("url") or ""),
                                           turn=turn, tool_index=tool_index)
            if name in {"open_result", "reopen_source", "open_hit"}:
                url = self._resolve_alias_url(name, args)
                if not url:
                    return ToolResult(call=call, model_text=f"ERROR: unknown {name} id"), False, "", [], None
                target = (getattr(self.session, "hit_targets", {}) or {}).get(
                    str(args.get("hit_id") or "").lower(), {}
                ) if name == "open_hit" else {}
                opened = (tools.open_url(url, offset=target.get("offset", 0),
                                         pages=target.get("pages", ""))
                          if target else tools.open_url(url))
                return self._opened_result(call, opened, url, turn=turn, tool_index=tool_index)
            if name == "source_search":
                query = str(args.get("query") or "")
                url = str(args.get("url") or "")
                if not url and str(args.get("source_id") or ""):
                    url = self._resolve_alias_url("reopen_source", {"source_id": args.get("source_id")}) or ""
                text = tools.source_search(url, query, args.get("limit", 6))
                if _is_ok_text(text) and url and self.session is not None:
                    text = self._attach_hit_ids(text, url)
                return ToolResult(call=call, model_text=text), _is_ok_text(text), "", [], None
            if name == "knowledge_search":
                from codey.research.text_args import first_text_arg

                text = tools.knowledge_search(first_text_arg(args, "query"))
                return ToolResult(call=call, model_text=text), _is_ok_text(text), "", [], None
            if name == "knowledge_read":
                text = tools.knowledge_read(str(args.get("id") or ""))
                return ToolResult(call=call, model_text=text), _is_ok_text(text), "", [], None
            if name == "knowledge_write":
                before = len(getattr(getattr(tools, "ledger", None), "evidence_items", ()) or ())
                lowered, problem = self._lower_knowledge_sources(args)
                if problem:
                    return ToolResult(call=call, model_text=f"ERROR: {problem}"), False, "", [], None
                text = tools.knowledge_write(lowered)
                if not _is_ok_text(text):
                    return ToolResult(call=call, model_text=text), False, "", [], None
                return ToolResult(call=call, model_text=text), True, "", self._ledger_evidence(before), None
            if name == "knowledge_link":
                text = tools.knowledge_link(str(args.get("src") or ""), str(args.get("dst") or ""),
                                            str(args.get("kind") or "relates"))
                return ToolResult(call=call, model_text=text), _is_ok_text(text), "", [], None
        except Exception as exc:
            return ToolResult(call=call, model_text=f"ERROR: {exc}"), False, "", [], None
        return ToolResult(call=call, model_text=f"ERROR: unknown research tool {name}"), False, "", [], None

    def _lower_knowledge_sources(self, args: dict) -> tuple[dict, str]:
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
        if isinstance(args.get("sources"), list):
            rows: list[str] = []
            for value in args["sources"]:
                resolved, error = lower(value)
                if error:
                    return {}, error
                rows.append(resolved)
            lowered["sources"] = rows
        if isinstance(args.get("evidence"), list):
            evidence: list[dict] = []
            for value in args["evidence"]:
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

    def _resolve_alias_url(self, alias: str, args: dict) -> str:
        session = self.session
        key = {"open_result": "result_id", "reopen_source": "source_id", "open_hit": "hit_id"}.get(alias, "")
        rid = str(args.get(key, "") or "").strip().lower() if key else ""
        if not rid or session is None:
            return ""
        results = dict(getattr(session, "search_results", {}) or {})
        sources = dict(getattr(session, "source_ids", {}) or {})
        if alias == "open_hit":
            return str((getattr(session, "hit_targets", {}) or {}).get(rid, {}).get("url", "") or "")
        return str(results.get(rid, "") or sources.get(rid, "") or "")

    def _attach_hit_ids(self, text: str, url: str) -> str:
        import re

        lines: list[str] = []
        for line in str(text or "").splitlines():
            match = re.match(r"^\s*\d+\.\s+(?:offset\s+(\d+)|p\.(\d+)):", line)
            if match:
                hit_id = self.session.record_hit(
                    url, offset=int(match.group(1) or 0), pages=match.group(2) or "",
                )
                line = f"{hit_id}: {line}"
            lines.append(line)
        return "\n".join(lines)

    def _ledger_final_url(self, args: dict) -> str:
        tools = self.research_tools
        ledger = getattr(tools, "ledger", None)
        if ledger is None:
            return str(args.get("url", "") or "")
        try:
            finals = ledger.final_url_set()
        except Exception:
            return str(args.get("url", "") or "")
        requested = str(args.get("url", "") or "")
        try:
            from codey.research.urls import opened_url

            canonical = opened_url(ledger, requested)
        except Exception:
            canonical = requested
        if canonical in finals:
            return canonical
        ordered = list(finals)
        return ordered[-1] if ordered else requested

    def _ledger_evidence(self, before: int) -> list[dict[str, str]]:
        tools = self.research_tools
        ledger = getattr(tools, "ledger", None)
        items = list(getattr(ledger, "evidence_items", ()) or [])
        fresh = items[before:] if before >= 0 else items
        evidence: list[dict[str, str]] = []
        for item in fresh:
            url = str(getattr(item, "source_url", "") or "").strip()
            excerpt = str(getattr(item, "excerpt", "") or "").strip()
            if url and excerpt:
                evidence.append({"source_url": url[:500], "excerpt": excerpt[:600]})
        return evidence


__all__ = ["ExecutionDelegate", "build_research_tools"]
