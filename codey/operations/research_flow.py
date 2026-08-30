from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from codey.operations.context import RunFrame, RunHooks
from codey.operations.result import ModeOutcome
from codey.ghost.work_queue import GhostWorkItem
from codey.knowledge.research_interest import (
    build_research_interest_candidates,
    candidate_to_topic_hint,
)
from codey.policies.permissions import allows_context_source, profile_for_name
from codey.research.browser_search import BrowserSearchProvider
from codey.research.completion_gate import RESEARCH_QUEUE_KINDS
from codey.research.connector_search import ConnectorAwareSearchProvider
from codey.research.context import ResearchContext, RunTraceResearchSink
from codey.research.evidence_followup import run_evidence_followup
from codey.research.evidence_ledger import EvidenceLedgerWriteResult
from codey.research.pipeline import (
    ResearchIterationRun,
    ResearchPipeline,
    ResearchPipelineConfig,
)
from codey.research.proof_quality import proof_review_trace_payload
from codey.research.query_planner import build_research_plan, research_plan_trace_payload
from codey.research.runner import ResearchRunner
from codey.research.topic_continuity import (
    CONTEXT_SOURCE_KEY as TOPIC_CONTINUITY_CONTEXT_SOURCE_KEY,
    MAX_TOPIC_CLAIM_REFS,
    project_topic_continuity,
)
from codey.runtime import cancellation
from codey.runtime.prompt_envelope import FailOpenPromptTrace


@dataclass(frozen=True)
class ResearchFlowDeps:
    state: Any
    knowledge_store: Any
    evidence_ledgers: Any
    search_factory: Callable[[], object]
    run_research_advisors: Callable | None
    ghost_continuity: Callable[..., object]


def record_research_result_trace(trace: Any | None, result: Any) -> None:
    if trace is None:
        return
    sink = FailOpenPromptTrace(trace)
    sink.call("record_permission_profile", "research", phase="research")
    sink.call(
        "record_research_notes",
        [
            *getattr(result, "notes_created", ()),
            *getattr(result, "notes_updated", ()),
            getattr(result, "synthesis_id", ""),
        ],
    )
    sink.call("record_research_sources", getattr(result, "opened_sources", ()))
    record = getattr(result, "research_record", None)
    if record is None:
        return
    summary = None
    to_summary_payload = getattr(record, "to_summary_payload", None)
    if callable(to_summary_payload):
        summary = to_summary_payload()
    elif isinstance(record, dict):
        summary = record
    if summary is not None:
        sink.call("record_research_record_summary", summary)


def record_evidence_ledger_write_trace(trace: Any | None, result: Any) -> None:
    if trace is None or result is None:
        return
    to_trace_payload = getattr(result, "to_trace_payload", None)
    if not callable(to_trace_payload):
        return
    FailOpenPromptTrace(trace).call("record_evidence_ledger_write", to_trace_payload())


def record_research_proof_review_trace(trace: Any | None, review: Any) -> None:
    if trace is None or review is None:
        return
    sink = FailOpenPromptTrace(trace)
    sink.call(
        "record_research_proof_review",
        proof_review_trace_payload(review),
    )
    sink.call("flush")


def record_research_plan_trace(
    trace: Any | None,
    review: Any,
    *,
    question: str = "",
) -> None:
    if trace is None or review is None:
        return
    try:
        plan = build_research_plan(review, question=question)
        payload = research_plan_trace_payload(plan)
    except Exception:
        return
    sink = FailOpenPromptTrace(trace)
    sink.call("record_research_plan", payload)
    sink.call("flush")


def default_research_search_provider() -> ConnectorAwareSearchProvider:
    return ConnectorAwareSearchProvider(BrowserSearchProvider(isolated=False))


def research_queue_item_title(item: GhostWorkItem | None) -> str:
    if item is None:
        return ""
    if str(getattr(item, "kind", "") or "") not in RESEARCH_QUEUE_KINDS:
        return ""
    return str(getattr(item, "title", "") or "").strip()


def run_research_mode(
    deps: ResearchFlowDeps,
    frame: RunFrame,
    hooks: RunHooks,
    *,
    proof_question: str = "",
    run_pipeline: Callable[..., object] | None = None,
) -> ModeOutcome:
    request = frame.request
    if frame.provider is None:
        raise RuntimeError("provider is not connected")
    pipeline = run_pipeline or run_research_pipeline
    pipeline_result = pipeline(
        frame,
        hooks,
        max_turns=request.max_turns,
        proof_question=proof_question,
    )
    result = pipeline_result.final_result
    deps.state.set_provider_session(
        frame.provider_id,
        None if result.stop_reason == "stopped" else request.session_id,
    )
    frame.conversation.begin_window(
        frame.provider_id,
        "research",
        frame.project_text,
    )
    frame.conversation.record_exchange(
        request.task,
        result.summary,
        replace(
            frame.conversation.snapshot,
            mode="research",
            goal=request.task,
            project=frame.project_text,
            provider_id=frame.provider_id,
            blocker="" if result.stop_reason == "done" else result.summary,
            latest_user=request.task,
            latest_reply=result.summary,
            summary=result.summary,
        ),
    )
    receipt = {
        "display": {"summary": result.receipt},
        "work": {
            "created": result.notes_created,
            "updated": result.notes_updated,
            "synthesis_id": result.synthesis_id,
        },
    }
    return ModeOutcome({
        "type": "task_done",
        "run_id": frame.run_id,
        "session_id": request.session_id,
        "summary": result.summary,
        "stop_reason": result.stop_reason,
        "turns": result.turns,
        "max_turns": request.max_turns,
        "provider": frame.provider_id,
        "mode": "research",
        "receipt": receipt,
        "research": research_payload(result, pipeline_result=pipeline_result),
    }, research_result=result, research_pipeline_result=pipeline_result)


def run_hybrid_mode(
    deps: ResearchFlowDeps,
    frame: RunFrame,
    work: object,
    hooks: RunHooks,
    *,
    config_result: object | None = None,
    run_project: Callable[..., ModeOutcome],
    run_pipeline: Callable[..., object] | None = None,
) -> ModeOutcome:
    request = frame.request
    if frame.provider is None:
        raise RuntimeError("provider is not connected")
    pipeline = run_pipeline or run_research_pipeline
    pipeline_result = pipeline(
        frame,
        hooks,
        max_turns=max(1, min(request.max_turns, 18)),
    )
    research_result = pipeline_result.final_result
    if research_result.stop_reason != "done":
        return ModeOutcome({
            "type": "task_done",
            "run_id": frame.run_id,
            "session_id": request.session_id,
            "summary": research_result.summary,
            "stop_reason": research_result.stop_reason,
            "turns": research_result.turns,
            "max_turns": request.max_turns,
            "provider": frame.provider_id,
            "mode": "research",
            "receipt": {"display": {"summary": research_result.receipt}},
            "research": research_payload(research_result, pipeline_result=pipeline_result),
        }, research_result=research_result, research_pipeline_result=pipeline_result)
    frame.fresh_chat = True
    frame.handoff = ""
    frame.conversation.update_snapshot(replace(
        frame.conversation.snapshot,
        mode="research",
        goal=request.task,
        project=frame.project_text,
        provider_id=frame.provider_id,
        summary=research_result.summary,
        blocker="",
        latest_user=request.task,
        latest_reply=research_result.summary,
    ))
    return run_project(
        frame,
        work,
        hooks,
        config_result=config_result,
        research_result=research_result,
        research_pipeline_result=pipeline_result,
    )


def run_research_iteration(
    deps: ResearchFlowDeps,
    *,
    provider,
    session_id: str,
    project: str,
    task: str,
    max_turns: int,
    on_event: Callable[[object], None],
    stop_flag,
    provider_id: str,
    run_id: str,
    chat_handoff: str,
    trace_recorder,
    search,
    tools=None,
    iteration_context: str = "",
    topic_continuity_context: str = "",
    topic_continuity_payload: dict[str, object] | None = None,
) -> ResearchIterationRun:
    if deps.knowledge_store is None:
        raise RuntimeError("Research is not configured")
    runner = ResearchRunner(
        provider,
        search,
        deps.knowledge_store,
        max_turns=max_turns,
        should_stop=stop_flag.is_set if stop_flag is not None else None,
        session_id=session_id,
        project=project,
        chat_handoff=chat_handoff,
        permission_profile="research",
        trace_recorder=trace_recorder,
        run_id=run_id,
        review_advisors=(
            (lambda pack: deps.run_research_advisors(
                selected_provider=provider,
                selected_provider_id=provider_id,
                pack=pack,
            ))
            if deps.run_research_advisors is not None
            else None
        ),
        tools=tools,
        iteration_context=iteration_context,
        topic_continuity_context=topic_continuity_context,
        topic_continuity_payload=topic_continuity_payload,
    )
    for event in runner.run(task):
        on_event(event)
    if runner.result is None:
        raise RuntimeError("research finished without a result")
    return ResearchIterationRun(result=runner.result, tools=runner.tools)


def build_research_topic_continuity(
    deps: ResearchFlowDeps,
    *,
    session_id: str,
    project: str,
    trace: Any | None = None,
    prior_claim_refs: Callable[..., tuple[dict[str, object], ...]] | None = None,
) -> tuple[str, dict[str, object] | None]:
    """Admit bounded Ghost-to-Research topic continuity."""

    try:
        profile = profile_for_name("research")
        if not allows_context_source(profile, TOPIC_CONTINUITY_CONTEXT_SOURCE_KEY):
            return "", None
        interest_hints = [
            candidate_to_topic_hint(candidate)
            for candidate in build_research_interest_candidates(
                deps.knowledge_store,
                session_id=session_id,
                project=project,
            )
        ]
        continuity = deps.ghost_continuity(project=project, session_id=session_id)
        claim_ref_loader = prior_claim_refs or (
            lambda **kwargs: deps_prior_claim_refs(deps=deps, **kwargs)
        )
        projection = project_topic_continuity(
            interest_hints=interest_hints,
            continuity_hints=tuple(getattr(continuity, "selected_items", ()) or ()),
            claim_refs=claim_ref_loader(session_id=session_id, project=project),
        )
    except cancellation.TaskCancelled:
        raise
    except cancellation.DeadlineExceeded:
        raise
    except Exception:
        FailOpenPromptTrace(trace).call(
            "warn",
            "research_topic_continuity_projection_failed",
        )
        return "", None
    payload = projection.to_payload()
    if not projection.admitted:
        return "", payload
    return projection.prompt_text, payload


def deps_prior_claim_refs(
    *,
    deps: ResearchFlowDeps | None = None,
    session_id: str,
    project: str,
) -> tuple[dict[str, object], ...]:
    ledgers = deps.evidence_ledgers if deps is not None else None
    return prior_claim_refs(ledgers, session_id=session_id, project=project)


def prior_claim_refs(
    evidence_ledgers: Any,
    *,
    session_id: str,
    project: str,
) -> tuple[dict[str, object], ...]:
    """Bounded claim refs from the durable evidence ledger; refs only."""

    if evidence_ledgers is None:
        return ()
    try:
        snapshot = evidence_ledgers.load(session_id=session_id, project=project)
    except Exception:
        return ()
    payload = getattr(snapshot, "payload", None)
    if not getattr(snapshot, "available", False) or not isinstance(payload, Mapping):
        return ()
    refs: list[dict[str, object]] = []
    for record in list(payload.get("records") or ())[-4:]:
        for claim_ref in record.get("claim_refs") or ():
            text = str(claim_ref or "").strip()
            if not text:
                continue
            refs.append({"ref": f"prior_claim:{text}"})
            if len(refs) > MAX_TOPIC_CLAIM_REFS:
                break
        if len(refs) > MAX_TOPIC_CLAIM_REFS:
            break
    return tuple(refs)


def build_research_context(
    deps: ResearchFlowDeps,
    frame: RunFrame,
    *,
    proof_question: str,
    max_turns: int,
    build_topic_continuity: Callable[..., tuple[str, dict[str, object] | None]] | None = None,
) -> ResearchContext:
    request = frame.request
    if build_topic_continuity is None:
        continuity_text, continuity_payload = build_research_topic_continuity(
            deps,
            session_id=request.session_id,
            project=frame.project_text,
            trace=frame.trace,
        )
    else:
        continuity_text, continuity_payload = build_topic_continuity(
            session_id=request.session_id,
            project=frame.project_text,
            trace=frame.trace,
        )
    return ResearchContext(
        question=request.task,
        session_id=request.session_id,
        run_id=frame.run_id,
        project=frame.project_text,
        provider_id=frame.provider_id,
        proof_question=proof_question,
        permission_profile="research",
        max_turns=max_turns,
        chat_handoff=frame.research_handoff,
        should_stop=deps.state.stop_flag.is_set,
        trace=RunTraceResearchSink(frame.trace),
        topic_continuity_context=continuity_text,
        topic_continuity_payload=continuity_payload,
    )


def run_research_pipeline(
    deps: ResearchFlowDeps,
    frame: RunFrame,
    hooks: RunHooks,
    *,
    max_turns: int,
    proof_question: str = "",
    run_iteration: Callable[..., ResearchIterationRun] | None = None,
    build_context: Callable[..., ResearchContext] | None = None,
    record_ledger_write: Callable[[RunHooks, EvidenceLedgerWriteResult], None] | None = None,
):
    request = frame.request
    if frame.provider is None:
        raise RuntimeError("provider is not connected")

    def iteration(
        *,
        task: str,
        max_turns: int,
        chat_handoff: str,
        search: object,
        tools=None,
        iteration_context: str = "",
        topic_continuity_context: str = "",
        topic_continuity_payload=None,
    ):
        runner = run_iteration or (lambda **kwargs: run_research_iteration(deps, **kwargs))
        return runner(
            provider=frame.provider,
            session_id=request.session_id,
            project=frame.project_text,
            task=task,
            max_turns=max_turns,
            on_event=hooks.on_event,
            stop_flag=deps.state.stop_flag,
            provider_id=frame.provider_id,
            run_id=frame.run_id,
            chat_handoff=chat_handoff,
            trace_recorder=frame.trace,
            search=search,
            tools=tools,
            iteration_context=iteration_context,
            topic_continuity_context=topic_continuity_context,
            topic_continuity_payload=topic_continuity_payload,
        )

    def followup(
        *,
        tools,
        plan,
        material,
        question: str,
        initial_summary: str = "",
        max_context_chars: int = 8000,
        should_stop=None,
    ):
        return run_evidence_followup(
            provider=frame.provider,
            tools=tools,
            plan=plan,
            material=material,
            question=question,
            initial_summary=initial_summary,
            max_context_chars=max_context_chars,
            should_stop=should_stop,
        )

    recorder = getattr(deps.state, "record_research_changes", None)
    changes_sink = recorder if callable(recorder) else None
    context_builder = build_context or (
        lambda active_frame, **kwargs: build_research_context(deps, active_frame, **kwargs)
    )
    ledger_sink = record_ledger_write or record_evidence_ledger_write
    pipeline = ResearchPipeline(
        context=context_builder(
            frame,
            proof_question=proof_question,
            max_turns=max_turns,
        ),
        run_iteration=iteration,
        search_factory=deps.search_factory,
        evidence_followup_runner=followup,
        evidence_ledgers=deps.evidence_ledgers,
        config=ResearchPipelineConfig(),
        ledger_event_sink=lambda result: ledger_sink(hooks, result),
        research_changes_sink=changes_sink,
    )
    return pipeline.run()


def record_evidence_ledger_write(
    hooks: RunHooks,
    result: EvidenceLedgerWriteResult,
) -> None:
    payload = result.to_trace_payload()
    hooks.append_ledger(
        lambda ledger: ledger.append(
            "evidence_ledger_write",
            ok=payload.get("ok"),
            skipped=payload.get("skipped"),
            reason_code=payload.get("reason_code"),
            ledger_ref=payload.get("ledger_ref"),
            record_id=payload.get("record_id"),
            counts=payload.get("counts"),
        )
    )


def research_payload(result: Any, *, pipeline_result: Any | None = None) -> dict:
    payload = {
        "max_turns_used": int(getattr(result, "max_turns_used", 0) or 0),
        "synthesis_id": result.synthesis_id,
        "notes_created": result.notes_created,
        "notes_updated": result.notes_updated,
        "sources_read": result.sources_read,
        "source_urls": result.source_urls,
        "queries": result.queries,
        "search_results": result.search_results,
        "opened_sources": result.opened_sources,
        "coverage": result.coverage,
        "citation_map": result.citation_map,
        "evidence_items": result.evidence_items,
        "counterpoints": result.counterpoints,
        "quality_warnings": result.quality_warnings,
    }
    if pipeline_result is not None:
        to_payload = getattr(pipeline_result, "to_payload", None)
        metadata = to_payload() if callable(to_payload) else {}
        if isinstance(metadata, dict):
            payload.update({
                "followup_applied": bool(metadata.get("followup_applied")),
                "followup_rounds": max(0, min(3, int(metadata.get("followup_rounds") or 0))),
                "pipeline_stop_reason": str(metadata.get("stop_reason") or ""),
                "planner_stop_reason": str(metadata.get("planner_stop_reason") or ""),
                "fresh_source_count": max(0, int(metadata.get("fresh_source_count") or 0)),
                "new_evidence_count": max(0, int(metadata.get("new_evidence_count") or 0)),
                "final_evidence_count": max(0, int(metadata.get("final_evidence_count") or 0)),
                "attempted_fresh_source_count": max(0, int(metadata.get("attempted_fresh_source_count") or 0)),
                "attempted_new_evidence_count": max(0, int(metadata.get("attempted_new_evidence_count") or 0)),
            })
    return payload


__all__ = [
    "ResearchFlowDeps",
    "build_research_context",
    "build_research_topic_continuity",
    "default_research_search_provider",
    "deps_prior_claim_refs",
    "prior_claim_refs",
    "record_evidence_ledger_write",
    "record_evidence_ledger_write_trace",
    "record_research_plan_trace",
    "record_research_proof_review_trace",
    "record_research_result_trace",
    "research_payload",
    "research_queue_item_title",
    "run_research_iteration",
    "run_hybrid_mode",
    "run_research_mode",
    "run_research_pipeline",
]
