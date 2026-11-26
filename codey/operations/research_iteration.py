"""Research pipeline iteration driven by the shared task turn kernel."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import TYPE_CHECKING

from codey.completion.contract import CompletionProof
from codey.durable.session import DurableSession
from codey.knowledge.changes import KnowledgeChanges
from codey.knowledge.note import clean_open_questions
from codey.operations.kernel_contracts import KernelResult
from codey.operations.research.research_continuation import ResearchContinuation
from codey.operations.research.synthesis_publication import _link_synthesis as _link_synthesis
from codey.operations.research.synthesis_publication import _persist_synthesis as _persist_synthesis
from codey.operations.research.synthesis_publication import _selected_note_sources as _selected_note_sources
from codey.operations.research.synthesis_publication import _stable_synthesis_id as _stable_synthesis_id
from codey.policies.task_policy import TaskPolicy
from codey.research.answer_report import AnswerReport
from codey.research.ledger import ResearchLedger
from codey.research.report_quality import ReportQualityReview
from codey.research.report_values import has_project_delivery, report_delivery_matches
from codey.research.source_gateway import _SearchProvider
from codey.research.tools import ResearchTools
from codey.runs.trace import RunTraceRecorder

if TYPE_CHECKING:
    from codey.operations.research.dependencies import ResearchFlowDeps
    from codey.operations.task_session import TaskSession
    from codey.workspace.revision import WorkspaceRevisionStore

from codey.memory.context import retrieve_context
from codey.operations.generation_boundary import durable_generation_boundary_callback
from codey.operations.kernel_request_builder import KernelRunRequestBuilder
from codey.operations.research.research_continuation import ResearchInvocation
from codey.operations.task_guidance import task_guidance_for_policy
from codey.operations.task_loop import (
    KernelExecutionDeps,
    KernelObservationDeps,
)
from codey.research.pipeline import ResearchIterationRun
from codey.research.run_result import ResearchRunResult
from codey.runtime.core.operation_payload import RuntimeOperationState
from codey.runtime.observe.events import RunEvent
from codey.workspace.context_selection import assemble_context_candidates
from codey.workspace.context_source import ContextCandidate


def _proposal_knowledge_refs(proposal: Mapping[str, object]) -> list[object]:
    refs = proposal.get("knowledge_refs", [])
    if not isinstance(refs, list):
        raise ValueError("knowledge_refs must be an array")
    return list(refs)


def _publication_proof(outcome: KernelResult, compiled: AnswerReport | None, history: ResearchContinuation, synthesis_id: str, publish: bool) -> CompletionProof | None:
    if compiled is None or not publish:
        return outcome.proof
    from codey.operations.research_publication import published_answer_version
    from codey.research.report_projection import published_completion

    version = published_answer_version(history, synthesis_id)
    if outcome.proof is None:
        raise ValueError("published research report has no completion proof")
    position: RuntimeOperationState = history.state()
    return published_completion(outcome.proof, compiled, synthesis_id=synthesis_id, result_ref=position.work_ref,
                                answer_version=version)


def _research_tools(deps: ResearchFlowDeps, search: object, session_id: str, project: str) -> ResearchTools:
    store = deps.knowledge_store
    if store is None:
        raise RuntimeError("Research is not configured")
    if not isinstance(search, _SearchProvider):
        raise TypeError("research requires a search provider")
    return ResearchTools(search=search, store=store, changes=KnowledgeChanges(root=store.root),
                         session_id=session_id, project=project)


def _result_context(ledger: ResearchLedger, compiled: AnswerReport | None,
                    quality: ReportQualityReview | None, include_coverage: bool,
                    ) -> tuple[dict[str, object], list[dict[str, object]]]:
    question_answers: list[object] = list(compiled.coverage) if include_coverage and compiled else []
    coverage: dict[str, object] = {**ledger.coverage_payload(), "question_answers": question_answers}
    citations: list[dict[str, object]]
    if compiled is not None:
        citations = [dict(row) for row in compiled.source_versions]
    elif quality is not None:
        citations = quality.citation_payload()
    else:
        citations = []
    return coverage, citations


def _project_revisions(deps: ResearchFlowDeps, session: TaskSession) -> WorkspaceRevisionStore | None:
    if not session.project or not session.policy.allows("project.write") or deps.managed_outputs is None:
        return None
    from codey.workspace.revision import WorkspaceRevisionStore

    revisions = WorkspaceRevisionStore(deps.managed_outputs.root.parent)
    workspace = revisions.current_state(session.project)
    session.set_workspace_state(workspace.revision, workspace.fingerprint)
    return revisions


def run_research_iteration(
    deps: ResearchFlowDeps,
    *,
    provider: object,
    session_id: str,
    project: str,
    task: str,
    max_turns: int,
    on_event: Callable[[RunEvent], None],
    stop_flag: Event | None,
    provider_id: str,
    run_id: str,
    chat_handoff: str,
    trace_recorder: RunTraceRecorder | None,
    search: object,
    tools: ResearchTools | None = None,
    iteration_context: str = "",
    topic_continuity_context: str = "",
    topic_continuity_payload: Mapping[str, object] | None = None,
    requested_capabilities: tuple[str, ...] = (),
    task_policy: TaskPolicy | None = None,
    controller_denied: tuple[str, ...] = (),
    writer_attempt: int = 1,
    invocation: ResearchInvocation | None = None,
    publish_synthesis: bool = True,
) -> ResearchIterationRun:
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import build_task_policy

    invocation = invocation or ResearchInvocation(writer_attempt, "research" if writer_attempt == 1 else "synthesize")
    writer_attempt = invocation.attempt
    if tools is None:
        tools = _research_tools(deps, search, session_id, project)
    from codey.operations.provider_session import ObservedProvider
    from codey.providers.requests import ProviderRequest, SendTextRequest, SendTurnRequest
    from codey.runtime.observe.prompt_envelope import FailOpenPromptTrace
    from codey.workspace.context_admission import request_scope, source_from_value
    from codey.workspace.context_epoch import context_epoch_id

    def observe_send(request: ProviderRequest) -> None:
        if not isinstance(request, (SendTextRequest, SendTurnRequest)):
            return
        prompt = request.prompt
        if (not isinstance(prompt, str) or not topic_continuity_context
                or topic_continuity_context not in prompt or not topic_continuity_payload
                or topic_continuity_payload.get("admitted") is not True):
            return
        FailOpenPromptTrace(trace_recorder).call(
            "record_research_topic_continuity", topic_continuity_payload,
            epoch_id=context_epoch_id(prompt),
        )

    provider = ObservedProvider(provider, observe_send)
    tools.context_provider = provider
    policy = task_policy or build_task_policy(
        SimpleNamespace(
            project=project,
            requested_capabilities=requested_capabilities,
            strict_research=True,
        ),
        task_kind="research",
        strict_research=True,
    )
    from codey.task.web_intent import explicit_web_intent

    session = TaskSession(
        policy=policy, task_kind="research", project=project,
        max_turns=max_turns, task_text=task,
        required_source_urls=explicit_web_intent(task).urls,
        controller_denied=tuple(controller_denied or ()),
        handoff="\n".join(x for x in (
            f"Conversation context from this chat:\n{chat_handoff}" if chat_handoff else "",
            iteration_context, topic_continuity_context,
        ) if x),
    )
    # A parent synthesis adopts actual ledger observations from completed
    # acquisition, without pretending those actions ran in this invocation.
    if writer_attempt > 1:
        session.research_questions = tools.ledger.questions
        for source in tools.ledger.opened_sources:
            session.record_open(source.final_url)
        for item in tools.ledger.evidence_items:
            session.record_evidence(item.source_url, item.excerpt)
        session.notes_saved = len(set(tools.created_ids + tools.updated_ids))
    intent_sink = None
    admitted = None
    if deps.runtime_mutations is not None and session_id and run_id:
        from codey.operations.task_effects import KernelEffectSink, admitted_durable

        admitted = admitted_durable(deps.state, run_id)
        intent_sink = KernelEffectSink(
            deps.runtime_mutations, session_id=session_id, run_id=run_id,
            provider_id=provider_id, phase="research",
            managed_outputs=deps.managed_outputs,
            durable=admitted,
        )
    history = ResearchContinuation(deps.runtime_mutations, deps.managed_outputs, durable=deps.state.durable,
                                   session_id=session_id, run_id=run_id)
    prerequisites = history.prerequisite_facts(before_attempt=writer_attempt, policy=policy)
    session.handoff += prerequisites.handoff
    durable: DurableSession = history.durable
    if ((stop_flag is not None and stop_flag.is_set())
            or durable.task(run_id).state.get("cancel_requested")):
        from codey.operations.research_publication import settle_cancelled_publications
        from codey.runtime.core.cancellation import TaskCancelled

        if settle_cancelled_publications(history, tools.store):
            raise TaskCancelled("research publication settled after cancellation")
    position: RuntimeOperationState = history.state()
    if (position.writer_attempt < writer_attempt
            or (position.writer_attempt == writer_attempt and position.leaf in {"accepted", "writer_running"}
                and not position.logical_turn)):
        provider.new_chat()
    memory = retrieve_context(deps.state.memory, task=task, project=project, session_id=session_id, run_id=run_id,
                              context="research")
    topic_source: ContextCandidate | None = source_from_value(
        key="research_topic_continuity",
        text=topic_continuity_context,
        budget=900,
        scope=request_scope(project, session_id),
        capability_id="research_topic_continuity",
    )
    sources: tuple[ContextCandidate, ...] = memory.sources + ((topic_source,) if topic_source is not None else ())
    context_text, context_spans = assemble_context_candidates(sources)
    revisions = _project_revisions(deps, session)
    outcome = history.kernel(
        session,
        invocation=invocation,
        request=KernelRunRequestBuilder(
                provider=provider,
                run_id=run_id,
                effect_scope=f"research:{writer_attempt}",
                provider_id=provider_id,
                user_task=task,
                stagnant_turns=5,
                context_scope=request_scope(project, session_id),
                context_text=context_text,
                memory_sources=sources,
                memory_spans=context_spans,
                memory_traces=memory.traces,
                collect_memory_candidates=memory.collect_candidates,
                attempt_validate_memory_sources=lambda actual: memory.valid(deps.state.memory),
                stop_flag=stop_flag,
                task_guidance=task_guidance_for_policy(session.policy, task_kind=session.task_kind),
            ).build(execution=KernelExecutionDeps(
                intent_sink=intent_sink,
                project_path=Path(project) if project else None,
                workspace_revision_store=revisions,
                research_tools=tools,
                permission_profile="coding_writer" if policy.allows("project.write") else "research",
            ), observation=KernelObservationDeps(
                on_boundary=durable_generation_boundary_callback(
                    durable=admitted,
                    session_id=session_id,
                    task_id=run_id,
                    sink=intent_sink,
                ),
                trace_recorder=trace_recorder,
                on_event=on_event,
                completion_context={
                    "run_id": run_id,
                    "question": task,
                    "project": project,
                    "research_ledger": tools.ledger,
                    "source_ids": session.source_ids,
                    "research_prerequisites": prerequisites,
                },
            )),
    )
    if writer_attempt > 1 and outcome.stop_reason == "stopped":
        from codey.runtime.core.cancellation import TaskCancelled

        raise TaskCancelled("research synthesis stopped")
    if outcome.stop_reason in {"recovery_failure", "controller_failure", "delivery_pending", "provider_failure"}:
        from codey.research.pipeline import ResearchContinuationError

        raise ResearchContinuationError(outcome.summary)
    ledger = tools.ledger
    synthesis_id = ""
    compiled = None
    if outcome.completed:
        compiled = outcome.research_report if policy.strict_research else None
        if compiled is not None and not report_delivery_matches(
                outcome.summary, compiled.text, combined=has_project_delivery(outcome.proof)):
            raise ValueError("settled report does not match accepted delivery")
        # Deterministic projection of the settled driver output; the stored
        # digest rejects changes to that original output on replay.
        from codey.operations.research_publication import publish_synthesis as publish_answer

        open_questions = compiled.open_questions if compiled else tuple(clean_open_questions(session.last_done_args.get("open_questions", ())))
        knowledge_refs: list[object] = [*_proposal_knowledge_refs(session.last_done_args), *(compiled.knowledge_refs if compiled else ())]
        synthesis_id = publish_answer(history,
            tools, task, compiled.text if compiled is not None else outcome.summary, session_id=session_id,
            project=project, run_id=run_id, on_event=on_event,
            open_questions=open_questions,
            knowledge_refs=knowledge_refs,
            finding_ids=compiled.finding_ids if compiled else (),
            accepted_report=compiled,
            policy=policy,
            projection="initial" if writer_attempt == 1 else f"writer-{writer_attempt}",
        ) if publish_synthesis else ""
    quality = None
    if compiled is None and outcome.summary and outcome.stop_reason == "done":
        from codey.research.report_quality import review_report_quality

        quality = review_report_quality(
            outcome.summary, ledger=ledger,
            opened_sources=set(tools.sources_read),
            search_result_urls=set(tools.search_result_urls),
        )
    record = None
    if compiled is not None:
        from codey.research.report_projection import project_accepted_report

        record = project_accepted_report(compiled, question=task, run_id=run_id, session_id=session_id,
                                         project=project, synthesis_id=synthesis_id)
    elif outcome.summary or ledger.opened_sources or ledger.evidence_items:
        from codey.research.object_model import build_research_record

        record = build_research_record(
            question=task, summary=outcome.summary, ledger=ledger, review=quality,
            run_id=run_id, session_id=session_id, project=project,
            synthesis_id=synthesis_id,
            stop_reason=outcome.stop_reason,
        )
    coverage, citation_map = _result_context(ledger, compiled, quality, outcome.completed and policy.strict_research)
    result = ResearchRunResult(
        question=task, summary=outcome.summary, stop_reason=outcome.stop_reason,
        turns=outcome.turns,
        queries=[item.query for item in ledger.searches],
        search_results=ledger.search_results_payload(),
        opened_sources=ledger.opened_sources_payload(),
        coverage=coverage,
        citation_map=citation_map,
        evidence_items=ledger.evidence_payload(),
        counterpoints=list(quality.counterpoints) if quality is not None else [],
        quality_warnings=["Interpretations remain unreviewed; question coverage is model reported."] if compiled else
                         list(quality.warnings) if quality is not None else [],
        notes_created=list(tools.created_ids),
        notes_updated=list(tools.updated_ids),
        links_created=tools.links_created,
        sources_read=len(tools.sources_read),
        source_urls=sorted(tools.sources_read),
        synthesis_id=synthesis_id,
        research_record=record,
        max_turns_used=max_turns,
        completion_proof=_publication_proof(outcome, compiled, history, synthesis_id, publish_synthesis),
        accepted_report=compiled,
    )
    return ResearchIterationRun(result=result, tools=tools)


__all__ = ["run_research_iteration"]
