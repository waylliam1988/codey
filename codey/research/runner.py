"""Research loop integrated into Codey's run lifecycle."""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field
from typing import Callable

from codey import cancellation
from codey.events import RunEvent
from codey.knowledge.changes import KnowledgeChanges
from codey.knowledge.note import KnowledgeNote
from codey.knowledge.store import KnowledgeStore
from codey.models import ToolResult
from codey.research.advisors import EvidenceNote, EvidencePack
from codey.research.report_quality import ReportQualityReview, review_report_quality
from codey.research.protocols import JsonToolCodec, ProtocolCodec
from codey.research.tools import ResearchTools

DEFAULT_MAX_TURNS = 14
MAX_PROTOCOL_ERRORS = 2
MAX_IDLE_TURNS = 2
RECENT_CONTEXT_LIMIT = 8
PROVIDER_SEND_POLL_INTERVAL = 0.1
_ACTIVITY = {
    "web_search": "searching the web",
    "open_url": "reading the page",
    "knowledge_search": "recalling local notes",
    "knowledge_read": "reading a note",
    "knowledge_write": "saving a note",
    "knowledge_link": "linking notes",
}


@dataclass
class ResearchRunResult:
    question: str
    summary: str
    stop_reason: str
    turns: int
    queries: list[str] = field(default_factory=list)
    search_results: list[dict] = field(default_factory=list)
    opened_sources: list[dict] = field(default_factory=list)
    coverage: dict = field(default_factory=dict)
    citation_map: list[dict] = field(default_factory=list)
    evidence_items: list[dict] = field(default_factory=list)
    counterpoints: list[str] = field(default_factory=list)
    quality_warnings: list[str] = field(default_factory=list)
    notes_created: list[str] = field(default_factory=list)
    notes_updated: list[str] = field(default_factory=list)
    links_created: int = 0
    sources_read: int = 0
    source_urls: list[str] = field(default_factory=list)
    synthesis_id: str = ""
    advisor_count: int = 0

    @property
    def receipt(self) -> str:
        return (
            f"{len(self.notes_created)} notes created, "
            f"{len(self.notes_updated)} notes updated, "
            f"{self.links_created} links, "
            f"{self.sources_read} sources read, "
            f"stop_reason={self.stop_reason}"
        )


class ResearchRunner:
    def __init__(
        self,
        provider,
        search,
        store: KnowledgeStore,
        *,
        max_turns: int = DEFAULT_MAX_TURNS,
        codec: ProtocolCodec | None = None,
        should_stop: Callable[[], bool] | None = None,
        diagnostics: object | None = None,
        session_id: str = "",
        project: str = "",
        chat_handoff: str = "",
        review_advisors: Callable[[EvidencePack], tuple[object, ...]] | None = None,
    ) -> None:
        self.provider = provider
        self.search = search
        self.store = store
        self.max_turns = max(1, max_turns)
        self.codec = codec or JsonToolCodec()
        self.should_stop = should_stop or (lambda: False)
        self.diagnostics = diagnostics
        self.session_id = session_id
        self.project = project
        self.chat_handoff = (chat_handoff or "").strip()
        self.review_advisors = review_advisors
        self.changes = KnowledgeChanges(root=store.root)
        self.tools = ResearchTools(
            search=search,
            store=store,
            changes=self.changes,
            diagnostics=diagnostics,
            session_id=session_id,
            project=project,
        )
        self.result: ResearchRunResult | None = None

    def run(self, question: str):
        question = (question or "").strip()
        if not question:
            self.result = ResearchRunResult(question="", summary="", stop_reason="empty", turns=0)
            yield RunEvent.info("empty question; nothing to research")
            yield self._done_event()
            return
        try:
            cancellation.check()
            self.provider.new_chat()
        except cancellation.TaskCancelled:
            self.result = ResearchRunResult(question=question, summary="", stop_reason="stopped", turns=0)
            yield RunEvent.info("stop requested")
            yield self._done_event()
            return
        message = self._intro(question)
        stop_reason = "max_turns"
        summary = ""
        protocol_errors = 0
        idle_turns = 0
        turn = 0
        stop_announced = False
        advisor_reviewed = False
        advisor_count = 0
        final_review: ReportQualityReview | None = None
        for turn in range(1, self.max_turns + 1):
            if self._stop_requested():
                stop_reason = "stopped"
                stop_announced = True
                yield RunEvent.info("stop requested")
                break
            try:
                reply = self._send_provider(message)
            except cancellation.TaskCancelled:
                stop_reason = "stopped"
                stop_announced = True
                yield RunEvent.info("stop requested")
                break
            plan = self.codec.parse(reply)
            yield RunEvent.turn_started(turn, reply, note=_plan_note(plan))
            if plan.protocol_error and not plan.calls and plan.control is None:
                protocol_errors += 1
                if protocol_errors > MAX_PROTOCOL_ERRORS:
                    stop_reason = "protocol"
                    break
                message = self.codec.repair_prompt()
                continue
            protocol_errors = 0
            results: list = []
            for index, call in enumerate(plan.calls):
                yield RunEvent.tool_started(turn, call, _ACTIVITY.get(call.name, "working"), index)
                try:
                    outcome = self._dispatch(call)
                except cancellation.TaskCancelled:
                    stop_reason = "stopped"
                    break
                yield RunEvent.tool_finished(turn, call, outcome, index)
                results.append((call, outcome))
                if self._stop_requested():
                    stop_reason = "stopped"
                    break
            if stop_reason == "stopped":
                if not stop_announced:
                    yield RunEvent.info("stop requested")
                break
            if plan.control is not None and plan.control.kind == "done":
                failed = [
                    r for r in results
                    if getattr(r[1], "output", "").startswith(("ERROR:", "NEEDS_OPEN:"))
                ]
                if failed:
                    message = self.codec.format_results(_tool_results(results)) + (
                        "\n\nResolve the ERROR or NEEDS_OPEN results before calling done. "
                        "Open cited pages with open_url before saving facts, and link only notes that exist."
                    )
                    continue
                summary_candidate = plan.control.body.strip()
                review = review_report_quality(
                    summary_candidate,
                    ledger=self.tools.ledger,
                    opened_sources=self.tools.sources_read,
                    search_result_urls=self.tools.search_result_urls,
                )
                if not review.ok:
                    message = self.codec.format_results(_tool_results(results)) + "\n\n" + review.message
                    continue
                if self.review_advisors is not None and not advisor_reviewed:
                    advisor_reviewed = True
                    advices = self._review_with_advisors(question, summary_candidate, review)
                    advisor_count = len(advices)
                    if advices:
                        yield RunEvent.info("research evidence review completed", names=f"{advisor_count} advisor(s)")
                        message = _advisor_followup_prompt(summary_candidate, advices)
                        continue
                yield RunEvent.info(review.message, warnings=list(review.warnings))
                summary = summary_candidate
                final_review = review
                stop_reason = "done"
                break
            if not plan.calls:
                idle_turns += 1
                if idle_turns > MAX_IDLE_TURNS:
                    stop_reason = "no_progress"
                    break
                message = self.codec.repair_prompt()
                continue
            idle_turns = 0
            message = self.codec.format_results(_tool_results(results))
        synthesis_id = ""
        if summary:
            synthesis_id = self._persist_synthesis(question, summary)
            if synthesis_id:
                yield RunEvent.info("saved synthesis", names=synthesis_id)
        self.result = ResearchRunResult(
            question=question,
            summary=summary,
            stop_reason=stop_reason,
            turns=turn,
            queries=[item.query for item in self.tools.ledger.searches],
            search_results=self.tools.ledger.search_results_payload(),
            opened_sources=self.tools.ledger.opened_sources_payload(),
            coverage=self.tools.ledger.coverage_payload(),
            citation_map=final_review.citation_payload() if final_review else [],
            evidence_items=self.tools.ledger.evidence_payload(),
            counterpoints=list(final_review.counterpoints) if final_review else [],
            quality_warnings=list(final_review.warnings) if final_review else [],
            notes_created=list(self.tools.created_ids),
            notes_updated=list(self.tools.updated_ids),
            links_created=self.tools.links_created,
            sources_read=len(self.tools.sources_read),
            source_urls=sorted(self.tools.sources_read),
            synthesis_id=synthesis_id,
            advisor_count=advisor_count,
        )
        yield self._done_event()

    def _dispatch(self, call):
        cancellation.check()
        args = call.args
        if call.name == "web_search":
            return _Outcome(self.tools.web_search(str(args.get("query") or "")))
        if call.name == "open_url":
            return _Outcome(self.tools.open_url(
                str(args.get("url") or ""),
                offset=args.get("offset", 0),
                limit=args.get("limit", 6000),
            ))
        if call.name == "knowledge_search":
            return _Outcome(self.tools.knowledge_search(str(args.get("query") or "")))
        if call.name == "knowledge_read":
            return _Outcome(self.tools.knowledge_read(str(args.get("id") or args.get("note_id") or "")))
        if call.name == "knowledge_write":
            output = self.tools.knowledge_write(args)
            return _Outcome(output, changed=output.startswith("saved "))
        if call.name == "knowledge_link":
            output = self.tools.knowledge_link(
                str(args.get("src") or ""),
                str(args.get("dst") or ""),
                str(args.get("kind") or "relates"),
            )
            return _Outcome(output, changed=output.startswith(("linked:", "updated link:")))
        return _Outcome.error(f"unknown tool: {call.name}")

    def _send_provider(self, message: str) -> str:
        try:
            cancellation.check()
            if getattr(self.provider, "thread_safe_send", False):
                reply = self._send_provider_cancellable(message)
            else:
                reply = self.provider.send(message)
            cancellation.check()
            return reply
        except cancellation.TaskCancelled:
            raise
        except Exception as exc:
            self._record_model_failure("send", exc)
            raise

    def _send_provider_cancellable(self, message: str) -> str:
        results: queue.Queue[object] = queue.Queue(maxsize=1)

        def work() -> None:
            try:
                results.put(self.provider.send(message))
            except Exception as exc:
                results.put(exc)

        thread = threading.Thread(target=work, name="codey-research-provider-send", daemon=True)
        thread.start()
        while True:
            try:
                result = results.get(timeout=PROVIDER_SEND_POLL_INTERVAL)
                break
            except queue.Empty:
                if self._stop_requested():
                    raise cancellation.TaskCancelled("task stopped")
        if isinstance(result, Exception):
            raise result
        return str(result or "")

    def _stop_requested(self) -> bool:
        if self.should_stop():
            return True
        try:
            cancellation.check()
        except cancellation.TaskCancelled:
            return True
        return False

    def _record_model_failure(self, action: str, error: object) -> None:
        if self.diagnostics is not None:
            try:
                self.diagnostics.record(
                    "model",
                    action,
                    error,
                    model=getattr(self.provider, "location", getattr(self.provider, "name", "")),
                )
            except Exception:
                pass

    def _intro(self, question: str) -> str:
        parts = [
            self.codec.system_prompt(),
            _recent_context(self.store, self.session_id),
            _chat_handoff_context(self.chat_handoff),
            f"Research question:\n{question}",
        ]
        return "\n\n".join(part for part in parts if part)

    def _persist_synthesis(self, question: str, summary: str) -> str:
        title = _synthesis_title(question)
        note = KnowledgeNote.create(
            type="synthesis",
            title=title,
            body=_synthesis_body(summary, self.tools.ledger),
            tags=["research", f"session:{self.session_id}" if self.session_id else "research"],
            sources=sorted(self.tools.sources_read),
            session_id=self.session_id,
            project=self.project,
        )
        try:
            self.store.write_note(note, changes=self.changes)
        except OSError:
            return ""
        if note.id not in self.tools.created_ids:
            self.tools.created_ids.append(note.id)
        for related_id in [*self.tools.created_ids, *self.tools.updated_ids]:
            if related_id and related_id != note.id:
                self.store.link(note.id, related_id, "derives", changes=self.changes)
        return note.id

    def _review_with_advisors(
        self,
        question: str,
        summary: str,
        review: ReportQualityReview,
    ) -> tuple[object, ...]:
        if self.review_advisors is None:
            return ()
        pack = EvidencePack(
            question=question,
            draft=summary,
            opened_urls=tuple(sorted(self.tools.sources_read)),
            search_result_urls=tuple(sorted(self.tools.search_result_urls)),
            citation_map=tuple(review.citation_payload()),
            evidence_items=tuple(self.tools.ledger.evidence_payload()),
            notes=self._evidence_notes(),
            coverage=self.tools.ledger.coverage_payload(),
            session_id=self.session_id,
            project=self.project,
            warnings=tuple(review.warnings),
        )
        try:
            return tuple(self.review_advisors(pack))
        except cancellation.TaskCancelled:
            raise
        except Exception:
            return ()

    def _evidence_notes(self) -> tuple[EvidenceNote, ...]:
        notes: list[EvidenceNote] = []
        seen: set[str] = set()
        for note_id in [*self.tools.created_ids, *self.tools.updated_ids]:
            if not note_id or note_id in seen:
                continue
            seen.add(note_id)
            try:
                note = self.store.read_note(note_id)
            except (OSError, ValueError):
                note = None
            if note is None:
                continue
            notes.append(
                EvidenceNote(
                    id=note.id,
                    type=note.type,
                    title=note.title,
                    body=note.body,
                    sources=tuple(note.sources),
                )
            )
        return tuple(notes)

    def _done_event(self) -> RunEvent:
        result = self.result
        assert result is not None
        return RunEvent.status(
            f"research done: {result.receipt}",
        )


def _plan_note(plan) -> str:
    if plan.control is not None and plan.control.kind == "done":
        return "(done)"
    if plan.calls:
        return "(" + ", ".join(call.name for call in plan.calls) + ")"
    if plan.protocol_error:
        return "(no tool call)"
    return ""


def _recent_context(store: KnowledgeStore, session_id: str, limit: int = RECENT_CONTEXT_LIMIT) -> str:
    try:
        rows = store.index.recent(limit, session_id=session_id)
    except Exception:
        rows = []
    if not rows:
        return "Your local knowledge library is empty; this is the first run."
    lines = ["You already have these local notes (reuse them with knowledge_read/knowledge_search):"]
    for row in rows:
        status = row.get("status") or "active"
        lines.append(f"- [{row['type']}] {row['title']} (id={row['id']}, {status})")
    return "\n".join(lines)


def _chat_handoff_context(handoff: str) -> str:
    text = (handoff or "").strip()
    if not text:
        return ""
    return (
        "Conversation context from this chat. Use this only as bounded background "
        "for the research question; do not mention handoff mechanics.\n"
        f"{text}"
    )


def _synthesis_title(question: str, limit: int = 80) -> str:
    text = " ".join((question or "").split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text or "Research synthesis"


def _advisor_followup_prompt(summary: str, advices: tuple[object, ...]) -> str:
    lines = [
        "Private research evidence review found issues or gaps in your draft.",
        "Do not mention hidden advisors, voting, MoA, consensus, or this private review to the user.",
        "Use these notes silently. If more evidence is needed, call web_search/open_url and update notes. Otherwise call done with a revised final report.",
        "",
        "Your draft:",
        summary.strip(),
        "",
        "Private evidence review notes:",
    ]
    for index, advice in enumerate(advices, 1):
        label = str(getattr(advice, "label", f"Advisor {index}") or f"Advisor {index}")
        text = str(getattr(advice, "text", advice) or "").strip()
        if text:
            lines.append(f"{label}:\n{text}")
    return "\n\n".join(lines)


def _synthesis_body(summary: str, ledger) -> str:
    body = (summary or "").strip()
    appendix = _ledger_appendix(ledger)
    if not appendix:
        return body
    return f"{body}\n\n{appendix}".strip()


def _ledger_appendix(ledger) -> str:
    if ledger is None:
        return ""
    lines = ["## Evidence Ledger"]
    opened = ledger.opened_sources_payload()
    if opened:
        lines.append("### Opened Sources")
        for index, item in enumerate(opened, 1):
            quality = item.get("quality") or {}
            quality_text = " · ".join(
                part for part in (
                    str(quality.get("level") or ""),
                    str(quality.get("kind") or ""),
                    str(quality.get("freshness") or ""),
                    str(quality.get("independent_group") or ""),
                ) if part
            )
            lines.append(
                f"- [{index}] {item.get('title') or item.get('final_url') or ''} - {item.get('final_url') or ''}"
                + (f" ({quality_text})" if quality_text else "")
            )
    evidence = ledger.evidence_payload()
    if evidence:
        lines.append("### Evidence Items")
        for item in evidence:
            lines.extend((
                f"- [{item.get('stance') or 'supports'}] {item.get('claim') or ''}",
                f"  source: {item.get('source_url') or ''}",
                f"  excerpt: {item.get('excerpt') or ''}",
            ))
    coverage = ledger.coverage_payload()
    if coverage.get("queries"):
        lines.append("### Search Coverage")
        for query in coverage.get("queries", []):
            lines.append(f"- query: {query}")
        skipped = coverage.get("skipped_results") or []
        if skipped:
            lines.append("  skipped:")
            for item in skipped[:8]:
                lines.append(f"  - {item.get('title') or item.get('url') or ''} ({item.get('reason') or 'skipped'})")
    return "\n".join(lines).strip()


class _Outcome:
    def __init__(self, output: str, *, changed: bool = False) -> None:
        self.output = output
        self.status = _outcome_status(output)
        self.ok = self.status != "error"
        self.exit_code = None
        self.changed = changed and self.status == "ok"
        self.truncated = False

    @classmethod
    def error(cls, message: str) -> "_Outcome":
        text = message if message.startswith("ERROR:") else f"ERROR: {message}"
        return cls(text)

    def first_line(self, limit: int) -> str:
        return next(iter(self.output.splitlines()), "")[:limit]


def _outcome_status(output: str) -> str:
    if output.startswith("ERROR:"):
        return "error"
    if output.startswith(("NEEDS_OPEN:", "SKIPPED:")):
        return "needs_action"
    return "ok"


def _tool_results(results: list[tuple[object, _Outcome]]) -> list[ToolResult]:
    return [
        ToolResult(call=call, output=outcome.output, truncated=outcome.truncated)
        for call, outcome in results
    ]
