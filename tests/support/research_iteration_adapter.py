"""Legacy research adapter for tests and manual benchmark probes.

Production code enters research through ``run_research_iteration``.  This
adapter keeps the old generator-shaped surface available to migration tests
without making that compatibility surface part of the production package.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from typing import Any

from codey.operations.research_iteration import run_research_iteration
from codey.research.text_args import first_text_arg
from codey.toolchain.runtime import ToolOutcome as _BaseOutcome


def render_research_repair_prompt(codec: Any, plan: Any, state: Any | None = None) -> str:
    del codec, state
    try:
        kind = str(getattr(plan, "protocol_error_kind", "") or "")
        error = str(getattr(plan, "protocol_error", "") or "invalid Research tool call")
    except Exception:
        kind, error = "", "invalid Research tool call"
    lines = [
        "Your last reply did not satisfy the Research tool contract.",
        f"Error: {error}",
        "",
    ]
    if kind == "too_many_tools":
        lines.extend([
            "Research executes exactly one action per turn.",
            "Choose one next action from the current allowed-actions block and reply with only that JSON object.",
            "Do not wrap JSON in a markdown code fence. Do not repeat the same JSON object twice.",
        ])
    elif kind == "invalid_args":
        lines.extend([
            "The tool name was recognized, but its arguments did not match the required schema.",
            "Fix the missing or invalid argument and reply with exactly one JSON object.",
        ])
    else:
        lines.extend([
            'Reply with exactly one JSON object using {"tool":"...","args":{...}} and no other text.',
        ])
    return "\n".join(lines)


class ResearchToolOutcome(_BaseOutcome):  # type: ignore[valid-type,misc]
    """Compatibility projection used by legacy research tests."""

    def __init__(self, model_text: str = "", ok: bool = True, **kwargs: Any) -> None:
        text = str(model_text or "")
        exact_ok = ok if type(ok) is bool else False
        status = str(kwargs.get("status", "") or "")
        if not status:
            if text.startswith(("SKIPPED:", "NEEDS_OPEN:")):
                status = "needs_action"
            elif text.startswith("ERROR:"):
                status = "error"
            else:
                status = "ok" if exact_ok else "error"
        try:
            super().__init__(
                model_text=text,
                ok=exact_ok,
                canonical=dict(kwargs.get("canonical", {}) or {}),
                presentation=dict(kwargs.get("presentation", {}) or {}),
                audit=dict(kwargs.get("audit", {}) or {}),
                error_code=str(kwargs.get("error_code", "") or ""),
                exit_code=kwargs.get("exit_code"),
                changed=bool(kwargs.get("changed", False)),
                truncated=bool(kwargs.get("truncated", False)),
            )
        except Exception:
            self.model_text = text
            self.ok = exact_ok
            self.changed = bool(kwargs.get("changed", False))
            self.truncated = bool(kwargs.get("truncated", False))
            self.presentation = dict(kwargs.get("presentation", {}) or {})
            self.audit = dict(kwargs.get("audit", {}) or {})
            self.canonical = dict(kwargs.get("canonical", {}) or {})
        with contextlib.suppress(Exception):
            self.status = status
        for key, value in kwargs.items():
            with contextlib.suppress(Exception):
                if not hasattr(self, key):
                    setattr(self, key, value)


class ResearchIteration:
    """Test-only generator adapter around the shared research entry."""

    def __init__(
        self,
        provider: Any,
        search: Any,
        store: Any,
        *,
        max_turns: int = 8,
        codec: Any | None = None,
        should_stop: Any | None = None,
        diagnostics: Any | None = None,
        session_id: str = "",
        project: str = "",
        chat_handoff: str = "",
        review_advisors: Any | None = None,
        permission_profile: str = "research",
        trace_recorder: Any = None,
        run_id: str = "",
        tools: Any | None = None,
        iteration_context: str = "",
        topic_continuity_context: str = "",
        topic_continuity_payload: Any | None = None,
        managed_outputs: Any = None,
        **_ignored: Any,
    ) -> None:
        self.provider = provider
        self.search = search
        self.store = store
        self.max_turns = max(1, int(max_turns or 8))
        self.codec = codec
        self.should_stop = should_stop or (lambda: False)
        self.diagnostics = diagnostics
        self.session_id = str(session_id or "")
        self.project = str(project or "")
        self.chat_handoff = str(chat_handoff or "")
        self.review_advisors = review_advisors
        self.permission_profile = str(permission_profile or "research")
        self.trace_recorder = trace_recorder
        self.run_id = str(run_id or "research-run")
        self.iteration_context = str(iteration_context or "")
        self.topic_continuity_context = str(topic_continuity_context or "")
        self.topic_continuity_payload = topic_continuity_payload
        self.managed_outputs = managed_outputs
        self._last_result: Any | None = None
        if tools is not None:
            self.tools = tools
        else:
            self.tools = self._build_tools()
        self.changes = getattr(self.tools, "changes", None)

    def _build_tools(self) -> Any | None:
        try:
            from codey.knowledge.changes import KnowledgeChanges
            from codey.research.tools import ResearchTools

            return ResearchTools(
                search=self.search,
                store=self.store,
                changes=KnowledgeChanges(root=getattr(self.store, "root", ".")),
                session_id=self.session_id,
                project=self.project,
            )
        except Exception:
            return None

    def _ensure_tools(self) -> Any:
        if self.tools is None:
            self.tools = self._build_tools()
        return self.tools

    def _dispatch(self, call: Any, turn: int = 0, tool_index: int = 0) -> Any:
        del turn, tool_index
        tools = self._ensure_tools()
        name = str(getattr(call, "name", "") or "").strip().lower()
        args = dict(getattr(call, "args", {}) or {})
        try:
            if name == "web_search":
                text = tools.web_search(first_text_arg(args, "query"))
                return ResearchToolOutcome(model_text=text, ok=not str(text or "").startswith("ERROR:"))
            if name == "open_url":
                opened = tools.open_url(
                    str(args.get("url") or ""),
                    offset=args.get("offset", 0),
                    limit=args.get("limit", 6000),
                    pages=str(args.get("pages") or ""),
                )
                from codey.research.tools import ResearchToolOutput

                if not isinstance(opened, ResearchToolOutput):
                    raise TypeError("ResearchTools.open_url must return ResearchToolOutput")
                text = str(getattr(opened, "model_text", opened) or "")
                title = next(
                    (line.removeprefix("Title: ") for line in text.splitlines() if line.startswith("Title: ")),
                    "",
                )
                presentation: dict[str, object] = {
                    "result": title or (text.splitlines()[0][:500] if text else "")
                }
                if text.startswith("SKIPPED:"):
                    presentation["status"] = "needs_action"
                return ResearchToolOutcome(
                    model_text=text,
                    ok=not text.startswith("ERROR:"),
                    presentation=presentation,
                )
            if name in {"knowledge_search", "knowledge_read", "knowledge_write", "knowledge_link", "source_search"}:
                if name == "source_search" and "query" not in args:
                    return ResearchToolOutcome(
                        model_text="ERROR: source_search missing required arg 'query'", ok=False
                    )
                coerced_args = dict(args)
                from codey.research.tool_contract import validate_tool_args

                validated = validate_tool_args(name, dict(args))
                if getattr(validated, "ok", False):
                    coerced_args = dict(getattr(validated, "args", {}) or {})
                else:
                    return ResearchToolOutcome(
                        model_text=f"ERROR: {getattr(validated, 'error', '') or f'{name} args invalid'}",
                        ok=False,
                    )
                fn = getattr(tools, name, None)
                if callable(fn):
                    try:
                        text = fn(**coerced_args) if coerced_args else fn()
                    except TypeError:
                        text = fn(coerced_args) if coerced_args else fn()
                    text_str = str(text or "")
                    blocked = text_str.startswith(("NEEDS_OPEN:", "SKIPPED:"))
                    changed = name in {"knowledge_write", "knowledge_link"} and not text_str.startswith("ERROR:") and not blocked
                    return ResearchToolOutcome(
                        model_text=text_str,
                        ok=not text_str.startswith("ERROR:"),
                        changed=changed,
                        presentation={"status": "needs_action"} if blocked else {},
                    )
        except TypeError:
            raise
        except Exception as exc:
            return ResearchToolOutcome(model_text=f"ERROR: {exc}")
        return ResearchToolOutcome(model_text=f"ERROR: unknown research tool {name or '?'}")

    def _intro(self, question: str) -> str:
        return "\n".join(x for x in (str(question or ""), self.topic_continuity_context) if x)

    def _send_provider(self, message: str) -> str:
        return str(self.provider.send(message))

    def run(self, question: str) -> Any:
        deps = SimpleNamespace(
            knowledge_store=self.store,
            managed_outputs=self.managed_outputs,
            runtime_mutations=None,
        )
        tools = self.tools or self._build_tools()
        iteration = run_research_iteration(
            deps,
            provider=self.provider,
            session_id=self.session_id or "research-session",
            project=self.project,
            task=str(question or ""),
            max_turns=self.max_turns,
            on_event=lambda _event: None,
            stop_flag=None,
            provider_id="local",
            run_id=self.run_id,
            chat_handoff=self.chat_handoff,
            trace_recorder=self.trace_recorder,
            search=self.search if callable(self.search) else (lambda: ""),
            tools=tools,
            iteration_context=self.iteration_context,
            topic_continuity_context=self.topic_continuity_context,
            topic_continuity_payload=self.topic_continuity_payload,
        )
        self.tools = iteration.tools or tools
        self.changes = getattr(self.tools, "changes", None)
        self._last_result = iteration.result
        self.result = iteration.result
        yield iteration.result

    @property
    def result(self) -> Any:
        return self._last_result

    @result.setter
    def result(self, value: Any) -> None:
        with contextlib.suppress(Exception):
            self._last_result = value


__all__ = [
    "ResearchIteration",
    "ResearchToolOutcome",
    "first_text_arg",
    "render_research_repair_prompt",
]
