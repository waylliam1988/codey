"""Production tool execution for the unified kernel (operations layer).

Project tools reuse the coding guards and readers (path safety, command
policy, shell approval surface); Research tools reuse ResearchTools, the
source gateway, and the evidence ledger. Every call yields a structured
``ExecutionResult``; completion facts come only from ``ok`` results and ledger
state, never from substring matching or model-supplied parameters.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codey.runtime.core.models import ToolCall, ToolResult


@dataclass(frozen=True)
class ExecutionResult:
    ok: bool
    model_text: str
    changed: bool = False
    exit_code: int | None = None
    opened_url: str = ""
    evidence: tuple[dict[str, str], ...] = ()
    error_code: str = ""
    approval_required: bool = False

    def to_tool_result(self, call: ToolCall) -> ToolResult:
        return ToolResult(call=call, model_text=self.model_text)


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
    ) -> None:
        self.session = session
        self.project_path = Path(str(project_path)).expanduser() if project_path else None
        if self.project_path is not None and not self.project_path.is_dir():
            self.project_path = None
        self.tool_fns = tool_fns
        self.research_tools = research_tools
        self.permission_profile = str(permission_profile or "coding_writer")
        self.approval_available = bool(approval_available)
        if self.tool_fns is None and self.project_path is not None:
            try:
                from codey.agents.tools import DEFAULT_TOOL_FNS
            except Exception:
                DEFAULT_TOOL_FNS = None  # type: ignore[assignment]
            self.tool_fns = DEFAULT_TOOL_FNS

    def handles(self, name: str) -> bool:
        lowered = str(name or "").strip().lower()
        try:
            from codey.toolchain.tool_spec import spec_for_tool
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
        return False

    def execute(self, call: ToolCall) -> tuple[ToolResult, bool, str, list[dict[str, str]], int | None]:
        name = str(call.name or "").strip().lower()
        try:
            from codey.toolchain.tool_spec import spec_for_tool
            spec = spec_for_tool(name)
            executor = spec.executor if spec is not None else ""
        except Exception:
            executor = ""
        if executor == "project":
            return self._execute_project(call)
        if executor in {"source", "knowledge"}:
            return self._execute_research(call)
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
            decision, _replay = evaluate_tool_call_policy_for(
                call,
                project=self.project_path,
                permission_profile=self.permission_profile,
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
                result = ToolResult(call=call, model_text=outcome.model_text)
                return result, bool(outcome.ok), "", [], None
            if name == "edit":
                outcome = self._execute_edit(call)
                result = ToolResult(call=call, model_text=outcome.model_text)
                return result, bool(outcome.ok), "", [], None
            if name == "run":
                outcome = self.tool_fns.execute_run_command(
                    self.project_path, str((call.args or {}).get("path", ".") or "."),
                    str((call.args or {}).get("command", "") or ""),
                    permission_profile=self.permission_profile, phase="writer",
                    tool_id=str(getattr(call, "call_id", "") or ""),
                )
                result = ToolResult(call=call, model_text=outcome.model_text)
                return result, bool(outcome.ok), "", [], getattr(outcome, "exit_code", None)
        except Exception as exc:
            result = ToolResult(call=call, model_text=f"ERROR: {exc}")
            return result, False, "", [], None
        result = ToolResult(call=call, model_text=f"ERROR: unsupported project tool {name}")
        return result, False, "", [], None

    def _execute_edit(self, call: ToolCall) -> Any:
        from codey.toolchain.runtime import ToolOutcome

        args = dict(call.args or {})
        if str(args.get("content", "") or ""):
            return self.tool_fns.write_file(self.project_path, str(args.get("path", "") or ""),
                                            str(args.get("content", "") or ""))
        replacements = args.get("replacements")
        blocks: list[dict[str, str]] = []
        if isinstance(replacements, list):
            for item in replacements:
                if isinstance(item, dict) and item.get("old_string") is not None:
                    blocks.append({"old_string": str(item.get("old_string") or ""),
                                   "new_string": str(item.get("new_string") or "")})
        elif args.get("old_string") is not None:
            blocks.append({"old_string": str(args.get("old_string") or ""),
                           "new_string": str(args.get("new_string") or "")})
        if not blocks:
            return ToolOutcome.error("edit needs content or exact replacements")
        try:
            from codey.toolchain.runtime import EditBlock

            edit_blocks = [EditBlock(old_string=b["old_string"], new_string=b["new_string"]) for b in blocks]
        except Exception:
            edit_blocks = blocks  # type: ignore[assignment]
        return self.tool_fns.edit_file(self.project_path, str(args.get("path", "") or ""), edit_blocks)

    def _execute_research(self, call: ToolCall) -> tuple[ToolResult, bool, str, list[dict[str, str]], int | None]:
        tools = self.research_tools
        name = str(call.name or "").strip().lower()
        args = dict(call.args or {})
        try:
            if name == "web_search":
                from codey.research.runner import first_text_arg

                text = tools.web_search(first_text_arg(args, "query"))
                return ToolResult(call=call, model_text=text), _is_ok_text(text), "", [], None
            if name == "open_url":
                opened = tools.open_url(str(args.get("url") or ""), offset=args.get("offset", 0),
                                        limit=args.get("limit", 6000), pages=str(args.get("pages") or ""))
                model_text = getattr(opened, "model_text", str(opened))
                if not _is_ok_text(model_text):
                    return ToolResult(call=call, model_text=model_text), False, "", [], None
                return ToolResult(call=call, model_text=model_text), True, self._ledger_final_url(args), [], None
            if name in {"open_result", "reopen_source", "open_hit"}:
                url = self._resolve_alias_url(name, args)
                if not url:
                    return ToolResult(call=call, model_text=f"ERROR: unknown {name} id"), False, "", [], None
                opened = tools.open_url(url)
                model_text = getattr(opened, "model_text", str(opened))
                if not _is_ok_text(model_text):
                    return ToolResult(call=call, model_text=model_text), False, "", [], None
                return ToolResult(call=call, model_text=model_text), True, self._ledger_final_url({"url": url}), [], None
            if name == "source_search":
                query = str(args.get("query") or "")
                url = str(args.get("url") or "")
                if not url and str(args.get("source_id") or ""):
                    url = self._resolve_alias_url("reopen_source", {"source_id": args.get("source_id")}) or ""
                text = tools.source_search(url, query, args.get("limit", 6))
                return ToolResult(call=call, model_text=text), _is_ok_text(text), "", [], None
            if name == "knowledge_search":
                from codey.research.runner import first_text_arg

                text = tools.knowledge_search(first_text_arg(args, "query"))
                return ToolResult(call=call, model_text=text), _is_ok_text(text), "", [], None
            if name == "knowledge_read":
                text = tools.knowledge_read(str(args.get("id") or ""))
                return ToolResult(call=call, model_text=text), _is_ok_text(text), "", [], None
            if name == "knowledge_write":
                before = len(getattr(getattr(tools, "ledger", None), "evidence_items", ()) or ())
                text = tools.knowledge_write(args)
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

    def _resolve_alias_url(self, alias: str, args: dict) -> str:
        session = self.session
        key = {"open_result": "result_id", "reopen_source": "source_id", "open_hit": "hit_id"}.get(alias, "")
        rid = str(args.get(key, "") or "").strip().lower() if key else ""
        if not rid or session is None:
            return ""
        results = dict(getattr(session, "search_results", {}) or {})
        sources = dict(getattr(session, "source_ids", {}) or {})
        return str(results.get(rid, "") or sources.get(rid, "") or "")

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


__all__ = ["ExecutionDelegate", "ExecutionResult"]
