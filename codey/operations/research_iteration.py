"""Research pipeline iteration driven by the shared task turn kernel."""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

from codey.research.pipeline import ResearchIterationRun
from codey.research.run_result import ResearchRunResult
from codey.research.text_args import first_text_arg
from codey.toolchain.runtime import ToolOutcome as _BaseOutcome


def _persist_synthesis(tools: Any, task: str, summary: str, *, session_id: str,
                       project: str, on_event: Callable[[object], None], open_questions: Any = ()) -> str:
    if not summary or getattr(tools, "store", None) is None or getattr(tools, "changes", None) is None:
        return ""
    from codey.knowledge.note import KnowledgeNote, clean_open_questions
    from codey.research.synthesis import run_concept_tags as _run_concept_tags
    from codey.research.synthesis import synthesis_body as _synthesis_body
    from codey.research.synthesis import synthesis_title as _synthesis_title
    from codey.runtime.observe.events import RunEvent

    note_ids = [*tools.created_ids, *tools.updated_ids]
    tags = ["research"]
    if session_id:
        tags.append(f"session:{session_id}")
    tags.extend(_run_concept_tags(tools.store, note_ids))
    note = KnowledgeNote.create(
        type="synthesis", title=_synthesis_title(task),
        body=_synthesis_body(summary, tools.ledger), tags=tags,
        sources=sorted(tools.sources_read), session_id=session_id, project=project,
        open_questions=clean_open_questions(open_questions)[:4],
    )
    try:
        tools.store.write_note(note, changes=tools.changes)
    except OSError:
        return ""
    if note.id not in tools.created_ids:
        tools.created_ids.append(note.id)
    for related_id in note_ids:
        if related_id and related_id != note.id:
            tools.store.link(note.id, related_id, "derives", changes=tools.changes)
    on_event(RunEvent.info("saved synthesis", names=note.id))
    return note.id


def run_research_iteration(
    deps: Any,
    *,
    provider: Any,
    session_id: str,
    project: str,
    task: str,
    max_turns: int,
    on_event: Callable[[object], None],
    stop_flag: Any,
    provider_id: str,
    run_id: str,
    chat_handoff: str,
    trace_recorder: Any,
    search: Any,
    tools: Any = None,
    iteration_context: str = "",
    topic_continuity_context: str = "",
    topic_continuity_payload: Any = None,
    requested_capabilities: tuple[str, ...] = (),
    controller_enabled: bool = True,
) -> ResearchIterationRun:
    from codey.operations.task_loop import run_task_kernel
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import build_task_policy

    if tools is None:
        from codey.knowledge.changes import KnowledgeChanges
        from codey.research.tools import ResearchTools

        store = getattr(deps, "knowledge_store", None)
        if store is None:
            raise RuntimeError("Research is not configured")
        tools = ResearchTools(
            search=search,
            store=store,
            changes=KnowledgeChanges(root=store.root),
            session_id=session_id,
            project=project,
        )
    provider.new_chat()
    policy = build_task_policy(
        SimpleNamespace(
            project=project,
            requested_capabilities=requested_capabilities,
            strict_research=True,
        ),
        task_kind="research",
        strict_research=True,
    )
    session = TaskSession(
        policy=policy, task_kind="research", project=project,
        max_turns=max_turns, task_text=task,
        handoff="\n".join(x for x in (
            f"Conversation context from this chat:\n{chat_handoff}" if chat_handoff else "",
            iteration_context, topic_continuity_context,
        ) if x),
    )
    if not controller_enabled:
        # Manual baselines disable controller narrowing: seed evidence so the
        # controller returns None (all tools visible) via the new entry.
        try:
            session.record_search("baseline")
            session.record_search_result("r1", "https://example.com/baseline")
            session.record_open("https://example.com/baseline")
            session.record_evidence("https://example.com/baseline", "baseline excerpt")
        except Exception:
            pass
    intent_sink = None
    active_provider = provider
    mutations = getattr(deps, "runtime_mutations", None)
    if mutations is not None and session_id and run_id:
        from codey.operations.task_effects import KernelEffectSink, KernelRecordedProvider

        mutations.mark_writer_running(session_id, run_id, provider_id=provider_id)
        intent_sink = KernelEffectSink(
            mutations, session_id=session_id, run_id=run_id,
            provider_id=provider_id, phase="research",
        )
        active_provider = KernelRecordedProvider(provider, intent_sink)
    outcome = run_task_kernel(
        session,
        provider=active_provider,
        run_id=run_id,
        effect_scope="research:1",
        provider_id=provider_id,
        project_path=project or None,
        research_tools=tools,
        managed_outputs=getattr(deps, "managed_outputs", None),
        session_id=session_id,
        permission_profile="coding_writer" if policy.allows("project.write") else "research",
        user_task=task,
        stop_flag=stop_flag,
        intent_sink=intent_sink,
        on_event=on_event,
        completion_context={
            "run_id": run_id,
            "question": task,
            "project": project,
            "research_ledger": tools.ledger,
            "source_ids": session.source_ids,
        },
    )
    ledger = tools.ledger
    synthesis_id = ""
    if outcome.completed:
        synthesis_id = _persist_synthesis(
            tools, task, outcome.summary, session_id=session_id,
            project=project, on_event=on_event,
            open_questions=session.last_done_args.get("open_questions", ()),
        )
    quality = None
    if outcome.summary and outcome.stop_reason == "done":
        from codey.research.report_quality import review_report_quality

        quality = review_report_quality(
            outcome.summary, ledger=ledger,
            opened_sources=set(tools.sources_read),
            search_result_urls=set(tools.search_result_urls),
        )
    record = None
    if outcome.summary or ledger.opened_sources or ledger.evidence_items:
        from codey.research.object_model import build_research_record

        record = build_research_record(
            question=task, summary=outcome.summary, ledger=ledger, review=quality,
            run_id=run_id, session_id=session_id, project=project,
            synthesis_id=synthesis_id,
            stop_reason=outcome.stop_reason,
        )
    result = ResearchRunResult(
        question=task, summary=outcome.summary, stop_reason=outcome.stop_reason,
        turns=outcome.turns,
        queries=[item.query for item in ledger.searches],
        search_results=ledger.search_results_payload(),
        opened_sources=ledger.opened_sources_payload(),
        coverage=ledger.coverage_payload(),
        citation_map=quality.citation_payload() if quality is not None else [],
        evidence_items=ledger.evidence_payload(),
        counterpoints=list(quality.counterpoints) if quality is not None else [],
        quality_warnings=list(quality.warnings) if quality is not None else [],
        notes_created=list(tools.created_ids),
        notes_updated=list(tools.updated_ids),
        links_created=tools.links_created,
        sources_read=len(tools.sources_read),
        source_urls=sorted(tools.sources_read),
        synthesis_id=synthesis_id,
        research_record=record,
        max_turns_used=max_turns,
    )
    return ResearchIterationRun(result=result, tools=tools)


def render_research_repair_prompt(codec: Any, plan: Any, state: Any | None = None) -> str:
    """Repair prompt for research protocol errors (moved from old runner)."""
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
            "Reply with exactly one JSON object using {\"tool\":\"...\",\"args\":{...}} and no other text.",
        ])
    return "\n".join(lines)


class ResearchToolOutcome(_BaseOutcome):  # type: ignore[valid-type,misc]
    """Research tool result with the shared runtime outcome shape."""

    def __init__(self, model_text: str = "", ok: bool = True, **kwargs: Any) -> None:
        text = str(model_text or "")
        exact_ok = ok if type(ok) is bool else False
        # Old SKIPPED/NEEDS_OPEN outcomes carried status needs_action.
        status = str(kwargs.get("status", "") or "")
        if not status:
            if text.startswith("SKIPPED:") or text.startswith("NEEDS_OPEN:"):
                status = "needs_action"
            elif text.startswith("ERROR:"):
                status = "error"
            else:
                status = "ok" if exact_ok else "error"
        try:
            super().__init__(  # type: ignore[call-arg]
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
            try:
                if not hasattr(self, key):
                    setattr(self, key, value)
            except Exception:
                pass


class ResearchIteration:
    """Research iteration facade over the shared task kernel.

    It owns research-specific tool construction and result projection while
    the model turn, authorization, execution and completion checks remain in
    the common kernel.
    """

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
        controller_enabled: bool = True,
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
        self.controller_enabled = bool(controller_enabled)
        self.permission_profile = str(permission_profile or "research")
        self.trace_recorder = trace_recorder
        self.run_id = str(run_id or "research-run")
        self.iteration_context = str(iteration_context or "")
        self.topic_continuity_context = str(topic_continuity_context or "")
        self.topic_continuity_payload = topic_continuity_payload
        self.managed_outputs = managed_outputs
        self._last_result: Any | None = None
        # Eager tools like the old Runner so `runner.tools.*` works right
        # after construction (behavior tests access tools without run()).
        if tools is not None:
            self.tools = tools
        else:
            try:
                from codey.knowledge.changes import KnowledgeChanges
                from codey.research.tools import ResearchTools

                changes = KnowledgeChanges(root=getattr(self.store, "root", "."))
                self.tools = ResearchTools(
                    search=self.search,
                    store=self.store,
                    changes=changes,
                    session_id=self.session_id,
                    project=self.project,
                )
            except Exception:
                self.tools = None
        # Keep the change ledger available to callers that render restorations.
        try:
            self.changes = getattr(self.tools, "changes", None)
        except Exception:
            self.changes = None

    def _ensure_tools(self) -> Any:
        if self.tools is not None:
            return self.tools
        try:
            from codey.knowledge.changes import KnowledgeChanges
            from codey.research.tools import ResearchTools

            changes = KnowledgeChanges(root=getattr(self.store, "root", "."))
            tools = ResearchTools(
                search=self.search,
                store=self.store,
                changes=changes,
                session_id=self.session_id,
                project=self.project,
            )
            self.tools = tools
            return tools
        except Exception:
            return None

    def _dispatch(self, call: Any, turn: int = 0, tool_index: int = 0) -> Any:
        tools = self._ensure_tools()
        name = str(getattr(call, "name", "") or "").strip().lower()
        args = dict(getattr(call, "args", {}) or {})
        try:
            if name == "web_search":
                from codey.research.text_args import first_text_arg

                text = tools.web_search(first_text_arg(args, "query"))
                return ResearchToolOutcome(model_text=text, ok=not str(text or "").startswith("ERROR:"))
            if name == "open_url":
                opened = tools.open_url(
                    str(args.get("url") or ""),
                    offset=args.get("offset", 0),
                    limit=args.get("limit", 6000),
                    pages=str(args.get("pages") or ""),
                )
                try:
                    from codey.research.tools import ResearchToolOutput as _RTO

                    if not isinstance(opened, _RTO):
                        raise TypeError("ResearchTools.open_url must return ResearchToolOutput")
                except TypeError:
                    raise
                except Exception:
                    pass
                text = str(getattr(opened, "model_text", opened) or "")
                title = next(
                    (line.removeprefix("Title: ") for line in text.splitlines() if line.startswith("Title: ")),
                    "",
                )
                presentation: dict[str, object] = {"result": title or (text.splitlines()[0][:500] if text else "")}
                if text.startswith("SKIPPED:"):
                    presentation["status"] = "needs_action"
                return ResearchToolOutcome(model_text=text, ok=not text.startswith("ERROR:"), presentation=presentation)
            if name in {"knowledge_search", "knowledge_read", "knowledge_write", "knowledge_link", "source_search"}:
                # Canonical query arg required for source_search (old contract):
                # "queries" plural is invalid via the new entry as well.
                if name == "source_search" and "query" not in args:
                    return ResearchToolOutcome(model_text="ERROR: source_search missing required arg 'query'", ok=False)
                # Coerce via the research contract (singleton_dict, defaults) like the
                # old runner did, so dict evidence becomes [dict] via the new tools.
                coerced_args = dict(args)
                try:
                    from codey.research.tool_contract import validate_tool_args as _validate_research

                    validated = _validate_research(name, dict(args))
                    if getattr(validated, "ok", False):
                        coerced_args = dict(getattr(validated, "args", {}) or {})
                    else:
                        return ResearchToolOutcome(model_text=f"ERROR: {getattr(validated, 'error', '') or f'{name} args invalid'}", ok=False)
                except Exception:
                    pass
                fn = getattr(tools, name, None)
                if callable(fn):
                    try:
                        # Tools take explicit kwargs; drop contract-filled defaults not in signature?
                        # ResearchTools methods accept **-style args via explicit params, so pass coerced.
                        text = fn(**coerced_args) if coerced_args else fn()
                    except TypeError:
                        try:
                            text = str(fn(coerced_args) if coerced_args else fn())
                        except Exception as exc2:
                            return ResearchToolOutcome(model_text=f"ERROR: {exc2}")
                    text_str = str(text or "")
                    # NEEDS_OPEN/SKIPPED block without change (needs_action, not error).
                    blocked = text_str.startswith("NEEDS_OPEN:") or text_str.startswith("SKIPPED:")
                    changed = bool(
                        name in {"knowledge_write", "knowledge_link"}
                        and not text_str.startswith("ERROR:")
                        and not blocked
                    )
                    presentation: dict[str, object] = {}
                    if blocked:
                        presentation["status"] = "needs_action"
                    return ResearchToolOutcome(
                        model_text=text_str,
                        ok=not text_str.startswith("ERROR:"),
                        changed=changed,
                        presentation=presentation,
                    )
        except TypeError:
            raise
        except Exception as exc:
            return ResearchToolOutcome(model_text=f"ERROR: {exc}")
        return ResearchToolOutcome(model_text=f"ERROR: unknown research tool {name or '?'}")

    def run(self, question: str) -> Any:
        dudeps = SimpleNamespace(
            knowledge_store=self.store,
            search_factory=(lambda: self.search) if callable(self.search) else (lambda: (lambda *a, **k: "")),
            managed_outputs=self.managed_outputs,
            runtime_mutations=None,
        )
        # Callable search objects (FakeSearch instances) are not factories;
        # wrap the instance directly via tools when provided.
        tools = self.tools
        if tools is None and self.search is not None and not callable(self.search):
            try:
                from codey.knowledge.changes import KnowledgeChanges
                from codey.research.tools import ResearchTools

                changes = KnowledgeChanges(root=getattr(self.store, "root", "."))
                tools = ResearchTools(
                    search=self.search,
                    store=self.store,
                    changes=changes,
                    session_id=self.session_id,
                    project=self.project,
                )
                self.tools = tools
            except Exception:
                tools = None
        on_event = lambda _e: None  # noqa: E731
        iteration = run_research_iteration(
            dudeps,
            provider=self.provider,
            session_id=self.session_id or "research-session",
            project=self.project or "",
            task=str(question or ""),
            max_turns=self.max_turns,
            on_event=on_event,
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
            controller_enabled=bool(getattr(self, "controller_enabled", True)),
        )
        self._last_result = iteration.result
        # Expose the latest bounded result for callers that consume the
        # generator-style iteration API.
        import contextlib as _ctx

        with _ctx.suppress(Exception):
            self.result = iteration.result  # type: ignore[attr-defined]
        # Yield once so callers can consume the iteration as a stream.
        yield iteration.result
        return

    @property
    def result(self) -> Any:
        return getattr(self, "_last_result", None)

    @result.setter
    def result(self, value: Any) -> None:
        with contextlib.suppress(Exception):
            self._last_result = value


__all__ = [
    "ResearchIteration",
    "ResearchToolOutcome",
    "first_text_arg",
    "render_research_repair_prompt",
    "run_research_iteration",
]
