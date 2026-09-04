"""Research loop integrated into Codey's run lifecycle."""

from __future__ import annotations

import queue
import threading
from collections import Counter
from dataclasses import dataclass, field, replace
from typing import Callable, Mapping

from codey.runtime import cancellation
from codey.workspace.context_epoch import context_epoch_id, context_source_ref
from codey.workspace.context_source import (
    ContextSource,
    RenderedContextSource,
    render_context_sources_with_metadata,
)
from codey.runtime.events import RunEvent
from codey.policies.permissions import allows_context_source, profile_for_name
from codey.runtime.prompt_envelope import (
    FailOpenPromptTrace,
    PromptEnvelope,
    PromptEnvelopeSection,
    RenderedPromptSection,
    record_provider_send_prompt,
)
from codey.knowledge.changes import KnowledgeChanges
from codey.knowledge.concept_schema import normalize_concept
from codey.knowledge.note import KnowledgeNote, clean_open_questions
from codey.knowledge.store import KnowledgeStore
from codey.runtime.models import ToolResult, normalized_managed_output
from codey.research.advisors import EvidenceNote, EvidencePack
from codey.research.controller import (
    ResearchController,
    ResearchControlState,
    controller_action_contract_hash,
    controller_system_prompt,
    controller_tool_example,
    format_controller_results,
)
from codey.research.done_finalizer import finalize_done_answer
from codey.research.object_model import ResearchRecord, build_research_record
from codey.research.report_quality import ReportQualityReview, review_report_quality
from codey.research.protocols import JsonToolCodec, ProtocolCodec
from codey.research.source_document import compact_pages
from codey.research.source_rendering import UNTRUSTED_SOURCE_END, UNTRUSTED_SOURCE_START
from codey.research.tool_contract import (
    PROTOCOL_DIRECT_ANSWER,
    PROTOCOL_DISALLOWED_TOOL,
    PROTOCOL_INVALID_ARGS,
    PROTOCOL_NATIVE_SEARCH_LEAK,
    PROTOCOL_NO_JSON,
    PROTOCOL_TOO_MANY_TOOLS,
    PROTOCOL_UNKNOWN_TOOL,
    TOOL_CONTRACTS,
    tool_example,
)
from codey.research.tools import ResearchTools, clone_research_tools
from codey.research.topic_continuity import (
    CONTEXT_SOURCE_KEY as TOPIC_CONTINUITY_SOURCE_KEY,
    DEFAULT_TOPIC_BUDGET_CHARS,
)

DEFAULT_MAX_TURNS = 14
COMPLETION_EXTENSION_TURNS = 4
MAX_EFFECTIVE_TURNS = 18
MAX_PROTOCOL_ERRORS = 2
MAX_IDLE_TURNS = 2
RECENT_CONTEXT_LIMIT = 8
PROVIDER_SEND_POLL_INTERVAL = 0.1
_ACTIVITY = {
    "web_search": "searching the web",
    "open_url": "reading the page",
    "source_search": "searching within the source",
    "knowledge_search": "recalling local notes",
    "knowledge_read": "reading a note",
    "knowledge_write": "saving a note",
    "knowledge_link": "linking notes",
}


def first_text_arg(args: dict, key: str) -> str:
    value = args.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, (list, tuple)):
        for item in value:
            text = str(item or "").strip()
            if text:
                return text
    if value not in (None, "") and not isinstance(value, (dict, list, tuple)):
        text = str(value).strip()
        if text:
            return text
    return ""


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
    research_record: ResearchRecord | None = None
    max_turns_used: int = DEFAULT_MAX_TURNS

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
        controller_enabled: bool = True,
        permission_profile: str = "research",
        trace_recorder=None,
        run_id: str = "",
        tools: ResearchTools | None = None,
        iteration_context: str = "",
        topic_continuity_context: str = "",
        topic_continuity_payload: Mapping[str, object] | None = None,
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
        self.run_id = run_id
        self.permission_profile = profile_for_name(permission_profile).name
        self.trace_recorder = trace_recorder
        self.prompt_trace = FailOpenPromptTrace(trace_recorder)
        self.chat_handoff = (chat_handoff or "").strip()
        self.iteration_context = (iteration_context or "").strip()
        self.topic_continuity_context = (topic_continuity_context or "").strip()
        self.review_advisors = review_advisors
        self.controller_enabled = bool(controller_enabled)
        self.controller = (
            ResearchController(
                include_source_search=bool(getattr(self.codec, "include_source_search", True)),
            )
            if self.controller_enabled
            else None
        )
        if tools is None:
            self.changes = KnowledgeChanges(root=store.root)
            self.tools = ResearchTools(
                search=search,
                store=store,
                changes=self.changes,
                diagnostics=diagnostics,
                session_id=session_id,
                project=project,
            )
        else:
            self.tools = clone_research_tools(
                tools,
                search=search,
                diagnostics=diagnostics,
                session_id=session_id,
                project=project,
            )
            self.changes = self.tools.changes
        self.result: ResearchRunResult | None = None
        # Monotonic per-send counter for prompt-surface send_ref when no
        # runtime effect exists (research has no provider effect store).
        self._research_send_seq = 0
        # Intro rows are projected at the provider-turn boundary, not at
        # assembly time: the controller appends its action block after the
        # intro is built, so only the exact outbound bytes define the shared
        # content-addressed epoch.
        self._pending_intro_sections: tuple[RenderedPromptSection, ...] = ()
        self._pending_context_sources: tuple[RenderedContextSource, ...] = ()
        # Digest-only admission row, projected at the send boundary together
        # with the intro rows it describes.
        self.topic_continuity_payload = topic_continuity_payload

    def run(self, question: str):
        question = (question or "").strip()
        self.prompt_trace.call(
            "record_permission_profile",
            self.permission_profile,
            phase="research",
        )
        try:
            include_source_search = bool(getattr(self.codec, "include_source_search", True))
            model_contract_hash = (
                controller_action_contract_hash(include_source_search=include_source_search)
                if self.controller is not None
                else self.codec.model_tool_contract_hash()
            )
            self.prompt_trace.call(
                "record_protocol_codec",
                str(getattr(self.codec, "name", "") or ""),
                phase="research",
                model_tool_contract_hash=model_contract_hash,
                runtime_tool_contract_hash=(
                    self.codec.model_tool_contract_hash()
                    if self.controller is not None
                    else ""
                ),
            )
            self.prompt_trace.call(
                "record_tool_contract_hash",
                model_contract_hash,
                phase="research",
            )
            if self.controller is not None:
                self.prompt_trace.call(
                    "record_runtime_tool_contract_hash",
                    self.codec.model_tool_contract_hash(),
                    phase="research",
                )
        except Exception:
            pass
        if not question:
            self.result = ResearchRunResult(
                question="",
                summary="",
                stop_reason="empty",
                turns=0,
                max_turns_used=self.max_turns,
            )
            yield RunEvent.info("empty question; nothing to research")
            yield self._done_event()
            return
        try:
            cancellation.check()
            self.provider.new_chat()
        except cancellation.TaskCancelled:
            self.result = ResearchRunResult(
                question=question,
                summary="",
                stop_reason="stopped",
                turns=0,
                max_turns_used=self.max_turns,
            )
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
        final_open_questions: list[str] = []
        turn_limit = self.max_turns
        extension_limit = max(
            turn_limit,
            min(MAX_EFFECTIVE_TURNS, turn_limit + COMPLETION_EXTENSION_TURNS),
        )
        for turn in range(1, extension_limit + 1):
            if turn > turn_limit:
                break
            if self._stop_requested():
                stop_reason = "stopped"
                stop_announced = True
                yield RunEvent.info("stop requested")
                break
            control_state = (
                self.controller.build_state(self.tools, turn=turn, max_turns=self.max_turns)
                if self.controller is not None
                else None
            )
            outbound = (
                self.controller.append_block(message, control_state)
                if self.controller is not None and control_state is not None
                else message
            )
            try:
                reply = self._send_provider(outbound)
            except cancellation.TaskCancelled:
                stop_reason = "stopped"
                stop_announced = True
                yield RunEvent.info("stop requested")
                break
            plan = (
                self.controller.parse_plan(self.codec, reply, control_state)
                if self.controller is not None and control_state is not None
                else self.codec.parse(reply)
            )
            yield RunEvent.turn_started(turn, reply, note=_plan_note(plan))
            if plan.protocol_error and not plan.calls and plan.control is None:
                protocol_errors += 1
                self.prompt_trace.call(
                    "record_protocol_error",
                    plan.protocol_error_kind,
                    phase="research",
                    turn=turn,
                    tool_name=str(getattr(plan, "protocol_tool_name", "") or ""),
                )
                if protocol_errors > MAX_PROTOCOL_ERRORS:
                    stop_reason = "protocol"
                    break
                # Count a repair prompt only when one actually goes out: a
                # terminal protocol failure never sends it.
                self.prompt_trace.call(
                    "record_protocol_repair_prompt",
                    plan.protocol_error_kind,
                    phase="research",
                    turn=turn,
                )
                message = render_research_repair_prompt(self.codec, plan, control_state)
                continue
            if plan.calls or plan.control is not None:
                self.prompt_trace.call(
                    "record_protocol_valid_turn",
                    turn,
                    phase="research",
                )
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
                if self.controller is not None:
                    self.controller.record_tool_outcome(
                        control_state,
                        call,
                        _outcome_model_text(outcome),
                    )
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
                    if _outcome_model_text(r[1]).startswith(("ERROR:", "NEEDS_OPEN:"))
                ]
                if failed:
                    turn_limit = _maybe_extend_completion_turns(
                        turn_limit,
                        extension_limit=extension_limit,
                        turn=turn,
                        tools=self.tools,
                    )
                    message = self._format_results(_tool_results(results)) + (
                        "\n\nResolve the ERROR or NEEDS_OPEN results before calling done. "
                        "Open cited source pages before saving facts, and link only notes that exist."
                    )
                    continue
                summary_candidate = plan.control.body.strip()
                finalized = finalize_done_answer(
                    summary_candidate,
                    self.tools.ledger,
                    source_ids=control_state.source_urls if control_state is not None else {},
                )
                if finalized.changed:
                    self.prompt_trace.call(
                        "record_research_done_compilation",
                        {
                            "reason": finalized.reason,
                            "source_count": finalized.source_count,
                        },
                    )
                summary_candidate = finalized.text.strip()
                review = review_report_quality(
                    summary_candidate,
                    ledger=self.tools.ledger,
                    opened_sources=self.tools.sources_read,
                    search_result_urls=self.tools.search_result_urls,
                )
                if not review.ok:
                    yield RunEvent.info(review.message)
                    turn_limit = _maybe_extend_completion_turns(
                        turn_limit,
                        extension_limit=extension_limit,
                        turn=turn,
                        tools=self.tools,
                    )
                    message = _quality_review_followup(
                        self.codec,
                        _tool_results(results),
                        review.message,
                        controller_enabled=self.controller is not None,
                    )
                    continue
                if self.review_advisors is not None and not advisor_reviewed:
                    advisor_reviewed = True
                    advices = self._review_with_advisors(question, summary_candidate, review)
                    advisor_count = len(advices)
                    if advices:
                        yield RunEvent.info("research evidence review completed", names=f"{advisor_count} advisor(s)")
                        turn_limit = _maybe_extend_completion_turns(
                            turn_limit,
                            extension_limit=extension_limit,
                            turn=turn,
                            tools=self.tools,
                        )
                        message = _advisor_followup_prompt(summary_candidate, advices)
                        continue
                yield RunEvent.info(review.message, warnings=list(review.warnings))
                summary = summary_candidate
                final_open_questions = _bounded_open_questions(
                    getattr(self.codec, "last_control_args", {}).get("open_questions")
                )
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
            message = self._format_results(_tool_results(results))
        synthesis_id = ""
        if summary:
            synthesis_id = self._persist_synthesis(question, summary, open_questions=final_open_questions)
            if synthesis_id:
                yield RunEvent.info("saved synthesis", names=synthesis_id)
        research_record = None
        if summary or self.tools.ledger.opened_sources or self.tools.ledger.evidence_items:
            research_record = build_research_record(
                question=question,
                summary=summary,
                ledger=self.tools.ledger,
                review=final_review,
                run_id=self.run_id,
                session_id=self.session_id,
                project=self.project,
                synthesis_id=synthesis_id,
                stop_reason=stop_reason,
            )
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
            research_record=research_record,
            max_turns_used=turn_limit,
        )
        self.prompt_trace.call(
            "record_research_notes",
            [
                *self.result.notes_created,
                *self.result.notes_updated,
                self.result.synthesis_id,
            ],
        )
        self.prompt_trace.call("record_research_sources", self.result.opened_sources)
        self.prompt_trace.call(
            "record_research_connector_errors",
            list(getattr(self.search, "last_connector_errors", [])),
        )
        if self.result.research_record is not None:
            self.prompt_trace.call(
                "record_research_record_summary",
                self.result.research_record.to_summary_payload(),
            )
        yield self._done_event()

    def _dispatch(self, call):
        cancellation.check()
        args = call.args
        if call.name == "web_search":
            return _Outcome(self.tools.web_search(first_text_arg(args, "query")))
        if call.name == "open_url":
            output = self.tools.open_url(
                str(args.get("url") or ""),
                offset=args.get("offset", 0),
                limit=args.get("limit", 6000),
                pages=str(args.get("pages") or ""),
            )
            return _Outcome(output, presentation_result=_opened_source_presentation(output))
        if call.name == "source_search":
            return _Outcome(self.tools.source_search(
                str(args.get("url") or ""),
                first_text_arg(args, "query"),
                args.get("limit", 6),
            ))
        if call.name == "knowledge_search":
            return _Outcome(self.tools.knowledge_search(first_text_arg(args, "query")))
        if call.name == "knowledge_read":
            return _Outcome(self.tools.knowledge_read(str(args.get("id") or "")))
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
            # Bind intro sections and admitted sources to this exact
            # outbound provider-send attempt first, so they share the epoch
            # stamped below.
            self._bind_pending_intro_rows(message)
            try:
                include_source_search = bool(getattr(self.codec, "include_source_search", True))
                model_hash = (
                    controller_action_contract_hash(include_source_search=include_source_search)
                    if self.controller is not None
                    else self.codec.model_tool_contract_hash()
                )
                runtime_hash = (
                    self.codec.model_tool_contract_hash()
                    if self.controller is not None
                    else ""
                )
            except Exception:
                model_hash = ""
                runtime_hash = ""
            self._research_send_seq += 1
            research_send_ref = f"research_send:{self._research_send_seq}"
            record_provider_send_prompt(
                self.trace_recorder,
                name="research_outbound_prompt",
                text=message,
                purpose="research prompt sent to provider",
                source_ref="provider_send:research",
                capability_id="research_runner",
                phase="research",
                send_ref=research_send_ref,
                provider_effect_id="",
                model_tool_contract_hash=model_hash,
                runtime_tool_contract_hash=runtime_hash,
            )
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

    def _topic_continuity_sources(self) -> tuple[RenderedContextSource, ...]:
        """Admit bounded topic continuity through the profile gate.

        Mirrors the coding-intro admission chain: ContextSource -> profile
        allow-list -> rendered metadata. Empty or gate-closed continuity
        renders to nothing instead of emitting an empty section.
        """
        sources = (
            ContextSource(
                key=TOPIC_CONTINUITY_SOURCE_KEY,
                loader=lambda: self.topic_continuity_context,
                budget=DEFAULT_TOPIC_BUDGET_CHARS,
                freshness="run_start",
                why_included="bounded local topic continuity leads, not evidence",
                capability_id="research_topic_continuity",
                admission_reason="run_start_assembly",
            ),
        )
        profile = profile_for_name(self.permission_profile)
        return render_context_sources_with_metadata(
            source for source in sources if allows_context_source(profile, source.key)
        ).sources

    def _intro(self, question: str) -> str:
        include_source_search = bool(getattr(self.codec, "include_source_search", True))
        system_prompt = (
            controller_system_prompt(include_source_search=include_source_search)
            if self.controller is not None
            else self.codec.system_prompt()
        )
        continuity = self._topic_continuity_sources()
        rendered = PromptEnvelope((
            PromptEnvelopeSection(
                name="research_system_prompt",
                text=system_prompt,
                purpose="research JSON tool protocol",
                freshness="run_start",
                source_refs=("protocol:research_json",),
            ),
            PromptEnvelopeSection(
                name="research_recent_context",
                text=_recent_context(self.store, self.session_id),
                purpose="bounded local research notes from this session",
                freshness="run_start",
                source_refs=("knowledge:recent_notes",),
            ),
            PromptEnvelopeSection(
                name="research_chat_handoff",
                text=_chat_handoff_context(self.chat_handoff),
                purpose="bounded chat handoff for research",
                freshness="run_start",
                source_refs=("conversation:research_handoff",),
            ),
            PromptEnvelopeSection(
                # Fully self-describing bounded text produced by
                # codey.research.topic_continuity; render() skips empty
                # sections so disabled continuity leaves the baseline intact.
                name="research_topic_continuity",
                text="\n\n".join(source.text for source in continuity),
                purpose="bounded local topic continuity, not evidence",
                freshness="run_start",
                source_refs=(
                    "local_context:research_topic_continuity",
                    *(context_source_ref(source.key) for source in continuity),
                ),
                budget=sum(source.budget for source in continuity),
                truncated=any(source.truncated for source in continuity),
            ),
            PromptEnvelopeSection(
                name="research_iteration_context",
                text=_iteration_context(self.iteration_context),
                purpose="bounded follow-up research material",
                freshness="run_start",
                source_refs=("research_pipeline:followup_material",),
            ),
            PromptEnvelopeSection(
                name="research_question",
                text=f"Research question:\n{question}",
                purpose="current research question",
                freshness="run_start",
                source_refs=("request:research_question",),
            ),
        )).render()
        # Binding happens at the send boundary, where the controller's
        # appended action block makes the bytes final (see
        # _bind_pending_intro_rows).
        self._pending_intro_sections = rendered.sections
        self._pending_context_sources = continuity
        return rendered.text

    def _bind_pending_intro_rows(self, outbound: str) -> None:
        """Project intro rows onto the exact outbound provider-send attempt.

        One content-addressed epoch binds every row of the first turn
        together: the assembled sections, the admitted context sources, and
        the outbound prompt recorded by record_provider_send_prompt() — all
        over the same bytes. Rows for intros that never enter a send-boundary
        projection are never emitted: nothing was bound to any outbound
        attempt.
        """
        if not self._pending_intro_sections and not self._pending_context_sources:
            return
        pending_sections = self._pending_intro_sections
        pending_sources = self._pending_context_sources
        self._pending_intro_sections = ()
        self._pending_context_sources = ()
        epoch = context_epoch_id(outbound)
        for section in pending_sections:
            self.prompt_trace.record_section(replace(section, epoch_id=epoch))
        if pending_sources:
            self.prompt_trace.call(
                "record_context_sources",
                pending_sources,
                epoch_id=epoch,
            )
        continuity_admitted = any(
            source.key == TOPIC_CONTINUITY_SOURCE_KEY
            for source in pending_sources
        )
        if continuity_admitted and self.topic_continuity_payload:
            # The admission row lands here, not at assembly time, and only
            # when the rendered source actually passed this runner's gate:
            # an admitted row is bound to the exact outbound provider-send
            # attempt that carried the continuity section (same sent-bytes
            # epoch). It proves what was bound to the outbound bytes, not
            # what the model ultimately processed.
            self.prompt_trace.call(
                "record_research_topic_continuity",
                self.topic_continuity_payload,
                epoch_id=epoch,
            )

    def _persist_synthesis(self, question: str, summary: str, *, open_questions: list[str] | None = None) -> str:
        title = _synthesis_title(question)
        tags = ["research"]
        if self.session_id:
            tags.append(f"session:{self.session_id}")
        tags.extend(
            _run_concept_tags(self.store, [*self.tools.created_ids, *self.tools.updated_ids])
        )
        note = KnowledgeNote.create(
            type="synthesis",
            title=title,
            body=_synthesis_body(summary, self.tools.ledger),
            tags=tags,
            sources=sorted(self.tools.sources_read),
            session_id=self.session_id,
            project=self.project,
            open_questions=open_questions or [],
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

    def _format_results(self, results: list[ToolResult]) -> str:
        if self.controller is not None:
            return format_controller_results(results)
        return self.codec.format_results(results)


def _plan_note(plan) -> str:
    if plan.control is not None and plan.control.kind == "done":
        return "(done)"
    if plan.calls:
        return "(" + ", ".join(call.name for call in plan.calls) + ")"
    if plan.protocol_error:
        kind = str(getattr(plan, "protocol_error_kind", "") or "").strip()
        return f"({kind or 'no tool call'})"
    return ""


def _maybe_extend_completion_turns(
    current_limit: int,
    *,
    extension_limit: int,
    turn: int,
    tools: ResearchTools,
) -> int:
    if current_limit >= extension_limit or int(turn or 0) < max(1, current_limit - 1):
        return current_limit
    if not _has_completion_material(tools):
        return current_limit
    return extension_limit


def _has_completion_material(tools: ResearchTools) -> bool:
    ledger = getattr(tools, "ledger", None)
    if ledger is not None and (
        getattr(ledger, "opened_sources", None)
        or getattr(ledger, "evidence_items", None)
    ):
        return True
    return bool(
        getattr(tools, "sources_read", None)
        or getattr(tools, "created_ids", None)
        or getattr(tools, "updated_ids", None)
    )


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


def _iteration_context(context: str) -> str:
    text = (context or "").strip()
    if not text:
        return ""
    return (
        "Bounded material from the Research pipeline. Use it as already opened "
        "research context for this follow-up synthesis; verify citations against "
        "the opened-source ledger before calling done.\n"
        f"{text}"
    )


def _quality_review_followup(
    codec: ProtocolCodec,
    results: list[ToolResult],
    message: str,
    *,
    controller_enabled: bool = False,
) -> str:
    action_hint = (
        "web_search/open_result/reopen_source/open_hit/knowledge_write"
        if controller_enabled
        else "web_search/open_url/knowledge_write"
    )
    prompt = (
        "Your last done.answer did not pass the research quality review.\n"
        f"{message}\n\n"
        "Before calling done again, check these hard requirements:\n"
        "- Supported conclusions need [n] citations in 结论 and 关键证据.\n"
        "- 来源 entries may be [1] Title - https://final-url or [1] https://final-url.\n"
        "- Each cited 来源 URL must be opened in this run and have saved evidence.\n"
        "- If no citable source exists, do not use [n] citations or list URLs in 来源; "
        "say no citable opened source was found and explain what was searched.\n\n"
        "Reply with exactly one JSON tool call. If more evidence is needed, call "
        f"{action_hint} first. If the issue is only in the "
        "final report wording, call done with a revised full report."
    )
    if not results:
        return prompt
    rendered = format_controller_results(results) if controller_enabled else codec.format_results(results)
    return rendered + "\n\n" + prompt


def render_research_repair_prompt(
    codec: ProtocolCodec,
    plan,
    state: ResearchControlState | None = None,
) -> str:
    kind = str(getattr(plan, "protocol_error_kind", "") or "")
    error = str(getattr(plan, "protocol_error", "") or "invalid Research tool call")
    lines = [
        "Your last reply did not satisfy the Research tool contract.",
        f"Error: {error}",
        "",
    ]
    if kind == PROTOCOL_TOO_MANY_TOOLS:
        lines.extend([
            "Research executes exactly one action per turn.",
            "Choose one next action from the current allowed-actions block and reply with only that JSON object.",
            "Do not wrap JSON in a markdown code fence. Do not repeat the same JSON object twice.",
        ])
        if state is None:
            lines.extend(["", "Example:", tool_example("knowledge_search")])
    elif kind == PROTOCOL_INVALID_ARGS:
        tool = _tool_from_protocol_error(error, state)
        lines.extend([
            "The tool name was recognized, but its arguments did not match the required schema.",
            "Fix the missing or invalid argument and reply with exactly one JSON object.",
        ])
        if state is not None:
            lines.append("Use one exact JSON shape from the current allowed-actions block.")
            if tool in state.allowed_tools:
                lines.extend(["", "Expected shape:", controller_tool_example(tool, state)])
        else:
            lines.extend([
                "",
                "Expected shape:",
                tool_example(tool) if tool else tool_example("web_search"),
            ])
    elif kind == PROTOCOL_DIRECT_ANSWER:
        lines.append("Do not write the research answer directly in prose.")
        if state is None or "done" in state.allowed_tools:
            lines.extend([
                "If the report is final and grounded in opened sources, return it through done.",
                "If more evidence is needed, call a local Research tool first.",
                "",
                "Final answer shape:",
                tool_example("done"),
            ])
        else:
            lines.extend([
                "done is not allowed yet because this run has no saved evidence.",
                "Choose one next action from the current allowed-actions block below.",
            ])
    elif kind == PROTOCOL_NATIVE_SEARCH_LEAK:
        lines.extend([
            "Do not use the chat website's own search, browsing, plugins, or outside knowledge.",
            "All web access in Research must go through local JSON tools.",
            "",
            "Use this shape:",
            tool_example("web_search"),
        ])
    elif kind == PROTOCOL_UNKNOWN_TOOL:
        if state is not None:
            lines.extend([
                f"Use only the current Research controller actions: {', '.join(state.allowed_tools)}.",
                "",
                "Example:",
                controller_tool_example(state.allowed_tools[0], state) if state.allowed_tools else tool_example("web_search"),
            ])
        else:
            lines.extend([
                f"Use only these Research tools: {_allowed_research_tools(codec)}.",
                "",
                "Example:",
                tool_example("web_search"),
            ])
    elif kind == PROTOCOL_DISALLOWED_TOOL:
        disallowed = _disallowed_tool_from_error(error)
        lines.extend([
            "The tool was valid JSON, but it is not allowed by the current Research controller state.",
            (
                f"Do not call {disallowed} again until it appears in the allowed tools list."
                if disallowed
                else "Do not call tools that are absent from the allowed tools list."
            ),
            "Use exactly one JSON shape from the current allowed-actions block.",
            "If you need evidence first, search/open sources before writing notes or calling done.",
        ])
    elif kind == PROTOCOL_NO_JSON:
        lines.extend([
            "Reply with a JSON tool call, not prose.",
            "",
            "Example:",
            (
                controller_tool_example(state.allowed_tools[0], state)
                if state is not None and state.allowed_tools
                else tool_example("knowledge_search")
            ),
        ])
    else:
        lines.append(codec.repair_prompt())
        return "\n".join(lines)
    lines.extend([
        "",
        "Reply with exactly one JSON object and no prose.",
    ])
    return "\n".join(lines)


def _tool_from_protocol_error(error: str, state: ResearchControlState | None = None) -> str:
    text = str(error or "")
    if state is not None:
        for tool in state.allowed_tools:
            if text.startswith(f"{tool}.") or text.startswith(f"{tool} "):
                return tool
    for tool in TOOL_CONTRACTS:
        if text.startswith(f"{tool}.") or text.startswith(f"{tool} "):
            return tool
    return ""


def _disallowed_tool_from_error(error: str) -> str:
    text = str(error or "").strip()
    marker = " is not allowed"
    if marker not in text:
        return ""
    tool = text.split(marker, 1)[0].strip()
    return tool if tool.replace("_", "").isalnum() else ""


def _allowed_research_tools(codec: ProtocolCodec) -> str:
    tools = [
        "web_search",
        "open_url",
        "knowledge_search",
        "knowledge_read",
        "knowledge_write",
        "knowledge_link",
        "done",
    ]
    if bool(getattr(codec, "include_source_search", True)):
        tools.insert(2, "source_search")
    return ", ".join(tools)


def _synthesis_title(question: str, limit: int = 80) -> str:
    text = " ".join((question or "").split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text or "Research synthesis"


def _run_concept_tags(store: KnowledgeStore, note_ids: list[str], limit: int = 5) -> list[str]:
    """Top concept tags from this run's notes, so synthesis joins the Concept Graph."""
    try:
        rows = store.index.tags_for([note_id for note_id in note_ids if note_id], active_only=True)
    except Exception:
        return []
    counts: Counter[str] = Counter()
    for row in rows:
        concept = normalize_concept(row.get("tag"))
        if concept:
            counts[concept] += 1
    return [concept for concept, _ in counts.most_common(limit)]


def _advisor_followup_prompt(summary: str, advices: tuple[object, ...]) -> str:
    lines = [
        "Private research evidence review found issues or gaps in your draft.",
        "Do not mention hidden advisors, voting, MoA, consensus, or this private review to the user.",
        "Use these notes silently. If more evidence is needed, search or open sources through the current Research controller actions and update notes. Otherwise call done with a revised final report.",
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


def _bounded_open_questions(value: object) -> list[str]:
    return clean_open_questions(value)[:4]


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
            source_meta = _source_meta(item, quality_text)
            lines.append(
                f"- [{index}] {item.get('title') or item.get('final_url') or ''} - {item.get('final_url') or ''}"
                + (f" ({source_meta})" if source_meta else "")
            )
    evidence = ledger.evidence_payload()
    if evidence:
        lines.append("### Evidence Items")
        for item in evidence:
            lines.extend((
                f"- [{item.get('stance') or 'supports'}] {item.get('claim') or ''}",
                f"  source: {item.get('source_url') or ''}{_evidence_locator(item)}",
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


def _source_meta(item: dict, quality_text: str = "") -> str:
    parts = []
    if quality_text:
        parts.append(quality_text)
    if str(item.get("content_kind") or "") == "pdf":
        parts.append("pdf")
        pages = compact_pages(item.get("pages_read") or ())
        page_count = int(item.get("page_count") or 0)
        if pages:
            parts.append(f"pages {pages}/{page_count}" if page_count else f"pages {pages}")
        if item.get("truncated"):
            parts.append("truncated")
    return " · ".join(parts)


def _evidence_locator(item: dict) -> str:
    locator = str(item.get("locator") or "")
    return f" {locator}" if locator else ""


class _Outcome:
    def __init__(self, model_text: str, *, changed: bool = False, presentation_result: str = "") -> None:
        self.model_text = model_text
        self.status = _outcome_status(model_text)
        self.ok = self.status != "error"
        self.exit_code = None
        self.changed = changed and self.status == "ok"
        self.truncated = False
        self.presentation = {"status": self.status, "result": (presentation_result or self.first_model_line(200))[:200]}
        self.audit = {}
        self.canonical = {}

    @classmethod
    def error(cls, message: str) -> "_Outcome":
        text = message if message.startswith("ERROR:") else f"ERROR: {message}"
        return cls(text)

    def first_model_line(self, limit: int) -> str:
        return next(iter(self.model_text.splitlines()), "")[:limit]

    def presentation_result(self, limit: int) -> str:
        value = self.presentation.get("result")
        text = str(value or "") or self.first_model_line(limit)
        return text[:limit]

    def presentation_status(self) -> str:
        value = self.presentation.get("status")
        return str(value or "") or ("ok" if self.ok else "error")

    def managed_output(self) -> dict[str, object]:
        value = self.audit.get("managed_output")
        return normalized_managed_output(value)


def _outcome_status(output: str) -> str:
    if output.startswith("ERROR:"):
        return "error"
    if output.startswith(("NEEDS_OPEN:", "SKIPPED:")):
        return "needs_action"
    return "ok"


def _opened_source_presentation(output: str) -> str:
    text = str(output or "")
    if UNTRUSTED_SOURCE_START not in text:
        return ""
    in_block = False
    for line in text.splitlines():
        clean = line.strip()
        if clean == UNTRUSTED_SOURCE_START:
            in_block = True
            continue
        if clean == UNTRUSTED_SOURCE_END:
            break
        if not in_block:
            continue
        if clean.startswith("Title:"):
            title = clean[len("Title:") :].strip()
            if title:
                return title
        if clean.startswith("URL:"):
            url = clean[len("URL:") :].strip()
            if url:
                return url
    return ""


def _outcome_model_text(outcome: _Outcome) -> str:
    return str(outcome.model_text or "")


def _tool_results(results: list[tuple[object, _Outcome]]) -> list[ToolResult]:
    return [
        ToolResult(
            call=call,
            model_text=outcome.model_text,
            truncated=outcome.truncated,
            presentation=outcome.presentation,
            audit=outcome.audit,
            canonical=outcome.canonical,
        )
        for call, outcome in results
    ]
