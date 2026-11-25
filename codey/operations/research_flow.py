from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from functools import partial
from threading import Event
from typing import Protocol

from codey.knowledge.changes import KnowledgeChanges
from codey.knowledge.recall import KnowledgeAccess, KnowledgeRecall
from codey.knowledge.research_interest import (
    build_research_interest_candidates,
    candidate_to_topic_hint,
)
from codey.knowledge.store import KnowledgeStore
from codey.operations.context import RunFrame, RunHooks
from codey.operations.result import ModeOutcome
from codey.operations.task_state import TaskState, stop_flag_for
from codey.policies.permissions import allows_context_source, profile_for_name
from codey.policies.task_policy import TaskPolicy
from codey.research.context import ResearchContext, RunTraceResearchSink
from codey.research.pipeline import (
    ResearchIterationRun,
    ResearchPipeline,
    ResearchPipelineResult,
)
from codey.research.proof_quality import ResearchProofReview, proof_review_trace_payload
from codey.research.query_planner import build_research_plan, research_plan_trace_payload
from codey.research.run_result import ResearchRunResult
from codey.research.tools import ResearchTools
from codey.research.topic_continuity import (
    CONTEXT_SOURCE_KEY as TOPIC_CONTINUITY_CONTEXT_SOURCE_KEY,
)
from codey.research.topic_continuity import (
    MAX_TOPIC_CLAIM_REFS,
    project_topic_continuity,
)
from codey.runs.trace import RunTraceRecorder
from codey.runtime.core import cancellation
from codey.runtime.observe.events import RunEvent
from codey.runtime.observe.prompt_envelope import FailOpenPromptTrace
from codey.runtime.write.mutation_line import RuntimeMutationLine
from codey.storage.managed_outputs import ManagedOutputStore


class ResearchPipelineRunner(Protocol):
    def __call__(self, frame: RunFrame, hooks: RunHooks, *, max_turns: int,
                 proof_question: str = "") -> ResearchPipelineResult: ...


class ClaimRefLoader(Protocol):
    def __call__(self, *, session_id: str, project: str) -> tuple[str, ...]: ...


class TopicContinuityBuilder(Protocol):
    def __call__(self, *, session_id: str, project: str,
                 trace: object | None = None) -> tuple[str, dict[str, object] | None]: ...


class ResearchContextBuilder(Protocol):
    def __call__(self, frame: RunFrame, *, proof_question: str, max_turns: int) -> ResearchContext: ...


class ResearchIterationRunner(Protocol):
    def __call__(self, *, provider: object, session_id: str, project: str, task: str,
                 max_turns: int, on_event: Callable[[RunEvent], None], stop_flag: Event | None,
                 provider_id: str, run_id: str, chat_handoff: str, trace_recorder: RunTraceRecorder | None,
                 search: object, topic_continuity_context: str = "",
                 topic_continuity_payload: Mapping[str, object] | None = None,
                 requested_capabilities: tuple[str, ...] = (), task_policy: TaskPolicy | None = None,
                 publish_synthesis: bool = True) -> ResearchIterationRun: ...


@dataclass(frozen=True)
class ResearchFlowDeps:
    state: TaskState
    knowledge_store: KnowledgeStore | None
    search_factory: Callable[[], object]
    managed_outputs: ManagedOutputStore | None = None
    runtime_mutations: RuntimeMutationLine | None = None


def record_research_proof_review_trace(trace: object | None, review: ResearchProofReview | None) -> None:
    if trace is None or review is None:
        return
    sink = FailOpenPromptTrace(trace)
    sink.call(
        "record_research_proof_review",
        proof_review_trace_payload(review),
    )
    sink.call("flush")


def record_research_plan_trace(
    trace: object | None,
    review: ResearchProofReview | None,
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




def run_research_mode(
    deps: ResearchFlowDeps,
    frame: RunFrame,
    hooks: RunHooks,
    *,
    proof_question: str = "",
    run_pipeline: ResearchPipelineRunner | None = None,
) -> ModeOutcome:
    request = frame.request
    if frame.provider is None:
        raise RuntimeError("provider is not connected")
    from codey.operations.provider_session import ConversationProvider

    frame.provider = ConversationProvider(
        frame.provider, frame.conversation,
        fresh_window=(frame.provider_id, "research", frame.project_text),
    )
    pipeline = run_pipeline or partial(run_research_pipeline, deps)
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
    frame.conversation.update_snapshot(
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
        "research": research_payload(
            result,
            pipeline_result=pipeline_result,
            restoreable=frame.run_id in getattr(deps.state, "research_changes", {}),
        ),
    }, research_result=result, research_pipeline_result=pipeline_result)


def build_research_topic_continuity(
    deps: ResearchFlowDeps,
    *,
    session_id: str,
    project: str,
    trace: object | None = None,
    prior_claim_refs: ClaimRefLoader | None = None,
) -> tuple[str, dict[str, object] | None]:
    """Admit bounded research topic continuity from core-owned knowledge."""

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
        claim_ref_loader = prior_claim_refs or (
            partial(recent_claim_refs, deps.knowledge_store)
        )
        projection = project_topic_continuity(
            interest_hints=interest_hints,
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


def recent_claim_refs(store: KnowledgeStore | None, *, session_id: str, project: str) -> tuple[str, ...]:
    if store is None:
        return ()
    access = KnowledgeAccess(project, session_id, store.owner_id)
    return KnowledgeRecall(store.repository).recent_claim_refs(access=access, limit=MAX_TOPIC_CLAIM_REFS + 1)


def build_research_context(
    deps: ResearchFlowDeps,
    frame: RunFrame,
    *,
    proof_question: str,
    max_turns: int,
    build_topic_continuity: TopicContinuityBuilder | None = None,
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
    run_iteration: ResearchIterationRunner | None = None,
    build_context: ResearchContextBuilder | None = None,
) -> ResearchPipelineResult:
    request = frame.request
    if frame.provider is None:
        raise RuntimeError("provider is not connected")

    def iteration(
        *,
        task: str,
        max_turns: int,
        chat_handoff: str,
        search: object,
        topic_continuity_context: str = "",
        topic_continuity_payload: Mapping[str, object] | None = None,
    ) -> ResearchIterationRun:
        import functools

        from codey.operations.research_iteration import run_research_iteration as _canonical

        runner = run_iteration or functools.partial(_canonical, deps)
        return runner(
            provider=frame.provider,
            session_id=request.session_id,
            project=frame.project_text,
            task=task,
            max_turns=max_turns,
            on_event=hooks.on_event,
            stop_flag=stop_flag_for(deps.state, frame.run_id),
            provider_id=frame.provider_id,
            run_id=frame.run_id,
            chat_handoff=chat_handoff,
            trace_recorder=frame.trace,
            search=search,
            topic_continuity_context=topic_continuity_context,
            topic_continuity_payload=topic_continuity_payload,
            requested_capabilities=request.requested_capabilities,
            task_policy=getattr(frame, "entry_policy", None),
            publish_synthesis=True,
        )

    recorder = getattr(deps.state, "record_research_changes", None)
    changes_sink = recorder if callable(recorder) else None
    context_builder = build_context or (
        partial(build_research_context, deps)
    )

    pipeline = ResearchPipeline(
        context=context_builder(
            frame,
            proof_question=proof_question,
            max_turns=max_turns,
        ),
        run_iteration=iteration,
        search_factory=deps.search_factory,
        research_changes_sink=changes_sink,
    )
    return pipeline.run()


def research_payload(
    result: ResearchRunResult,
    *,
    pipeline_result: ResearchPipelineResult | None = None,
    restoreable: bool | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "max_turns_used": result.max_turns_used,
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
        "semantic_review": result.semantic_review,
    }
    if result.completion_proof is not None:
        payload["completion_proof"] = result.completion_proof.to_payload()
    if pipeline_result is not None:
        payload.update({
            "pipeline_stop_reason": pipeline_result.stop_reason,
            "final_evidence_count": pipeline_result.final_evidence_count,
        })
    if restoreable is not None:
        payload["restoreable"] = restoreable
    return payload


def research_payload_from_tools(
    tools: ResearchTools,
    *,
    question: str,
    summary: str,
    stop_reason: str,
    turns: int,
    synthesis_id: str = "",
) -> dict[str, object]:
    """Project facts already collected by a hybrid run into the UI DTO.

    Hybrid execution shares ``ResearchTools`` with the kernel, so constructing
    this payload from its ledger avoids a second search or a second quality
    decision.  Note references remain knowledge note IDs from the tool state.
    """
    from codey.research.run_result import ResearchRunResult

    ledger = tools.ledger
    if ledger is None:
        raise ValueError("hybrid research tools have no ledger")
    searches = list(ledger.searches)
    result = ResearchRunResult(
        question=question,
        summary=summary,
        stop_reason=stop_reason,
        turns=max(0, (turns or 0)),
        queries=[item.query for item in searches if item.query],
        search_results=list(ledger.search_results_payload()),
        opened_sources=list(ledger.opened_sources_payload()),
        coverage=dict(ledger.coverage_payload()),
        evidence_items=list(ledger.evidence_payload()),
        notes_created=[str(item) for item in tools.created_ids],
        notes_updated=[str(item) for item in tools.updated_ids],
        links_created=max(0, tools.links_created),
        sources_read=len(tools.sources_read),
        source_urls=sorted(str(item) for item in tools.sources_read if item),
        synthesis_id=synthesis_id,
    )
    payload = research_payload(result)
    payload["restoreable"] = tools.changes.has_changes() if isinstance(tools.changes, KnowledgeChanges) else False
    return payload


__all__ = [
    "ResearchFlowDeps",
    "build_research_context",
    "build_research_topic_continuity",
    "recent_claim_refs",
    "record_research_plan_trace",
    "record_research_proof_review_trace",
    "research_payload",
    "research_payload_from_tools",
    "run_research_mode",
    "run_research_pipeline",
]
