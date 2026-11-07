"""Research pipeline iteration driven by the shared task turn kernel."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from codey.operations.generation_boundary import durable_generation_boundary_callback
from codey.operations.ghost_context import ghost_control_enabled
from codey.operations.kernel_request_builder import KernelRunRequestBuilder
from codey.operations.research_continuation import ResearchInvocation
from codey.operations.task_guidance import task_guidance_for_policy
from codey.operations.task_loop import (
    KernelExecutionDeps,
    KernelObservationDeps,
)
from codey.research.pipeline import ResearchIterationRun
from codey.research.run_result import ResearchRunResult
from codey.runtime.observe.events import RunEvent


def _proposal_knowledge_refs(proposal: Mapping[str, object]) -> list[object]:
    refs = proposal.get("knowledge_refs", [])
    if not isinstance(refs, list):
        raise ValueError("knowledge_refs must be an array")
    return list(refs)


def _stable_synthesis_id(run_id: str) -> str:
    """同一 run 的合成笔记恒用同一身份：写入成功后交付失败，重试复用原 ID。"""
    import hashlib

    if not run_id:
        raise ValueError("synthesis requires a run identity")
    return "synthesis-" + hashlib.sha256(run_id.encode("utf-8")).hexdigest()


def _selected_note_sources(store: Any, note_ids: list[str]) -> set[str]:
    sources: set[str] = set()
    for identity in note_ids:
        note = store.read_note(identity)
        if note is not None:
            sources.update(note.sources)
    return sources


def _persist_synthesis(tools: Any, task: str, summary: str, *, session_id: str,
                       project: str, run_id: str,
                       on_event: Callable[[RunEvent], None], open_questions: Any = (),
                       policy: Any, projection: str = "initial", knowledge_refs: Any = (),
                       finding_ids: tuple[str, ...] = (), question_coverage: Any = (), accepted_report: Any = None,
                       semantic_review: Any = None, publisher: Callable[[dict[str, Any]], str] | None = None) -> str:
    """Persist one synthesis note under the parent task policy (required).

    ``policy=None`` denies like any non-allowing policy (fail-closed). The
    note id is stable per run: a retry after a successful write reuses the
    original id via store upsert instead of creating a duplicate note.
    """
    if not summary or getattr(tools, "store", None) is None or getattr(tools, "changes", None) is None:
        return ""
    allows = getattr(policy, "allows", None)
    try:
        permitted = bool(allows("knowledge.write")) if callable(allows) else False
    except Exception:
        permitted = False
    if not permitted:
        try:
            from codey.runtime.observe.events import RunEvent as _DeniedEvent
            on_event(_DeniedEvent.info("synthesis denied by task policy", names="policy_denied"))
        except Exception:
            pass
        return ""
    from codey.research.answer_report import AnswerReport

    if isinstance(accepted_report, AnswerReport):
        from codey.research.answer_archive import publication_command

        if accepted_report.text != summary or publisher is None:
            raise ValueError("whole answers require their original frozen publication effect")
        note_id = _stable_synthesis_id(run_id if projection == "initial" else run_id + ":" + projection)
        return publisher(publication_command(accepted_report, task=task, run_id=run_id, projection=projection,
            note_id=note_id, session_id=session_id, project=project, store=tools.store))
    import hashlib

    from codey.knowledge.archive import stable_identity
    from codey.knowledge.model import KnowledgeMutation, KnowledgeObject, canonical_json

    note_id = _stable_synthesis_id(run_id if projection == "initial" else run_id + ":" + projection)
    answer_id = stable_identity("answer", run_id, projection)
    summary_digest = hashlib.sha256(summary.encode("utf-8")).hexdigest()
    from codey.knowledge.note import KnowledgeNote, clean_open_questions
    from codey.research.synthesis import run_concept_tags as _run_concept_tags
    from codey.research.synthesis import synthesis_body as _synthesis_body
    from codey.research.synthesis import synthesis_title as _synthesis_title
    from codey.runtime.observe.events import RunEvent

    # Only explicit selections are answer dependencies. Opening or saving a
    # source does not make every finding a premise of this answer.
    if accepted_report is not None:
        if accepted_report.text != summary:
            raise ValueError("archive text differs from accepted report")
        finding_ids = accepted_report.finding_ids
        knowledge_refs = list(accepted_report.knowledge_refs)
        question_coverage = accepted_report.coverage
        open_questions = accepted_report.open_questions
    note_ids = list(finding_ids)
    selected_sources = ({row["url"] for row in accepted_report.source_versions} if accepted_report
                        else _selected_note_sources(tools.store, note_ids))
    tags = ["research", *([f"session:{session_id}"] if session_id else [])]
    if accepted_report is None:
        tags.extend(_run_concept_tags(tools.store, note_ids))
    from codey.knowledge.answer_refs import answer_references, answer_scope

    references = list(answer_references(knowledge_refs, tools.store, project=project, session_id=session_id))
    scope, scope_id = answer_scope(references, tools.store, project=project, session_id=session_id)
    publication_digest = hashlib.sha256(canonical_json({"schema": 1, "task": task, "summary": summary,
        "references": [{"id": ref.target, "revision": ref.revision} for ref in references],
        "findings": list(finding_ids), "coverage": list(question_coverage), "open_questions": list(open_questions),
        "scope": [scope, scope_id], "session_id": session_id, "project": project, "sources": sorted(selected_sources),
        "report": accepted_report.to_payload() if accepted_report is not None else None,
        "semantic_review": semantic_review}).encode()).hexdigest()
    previous = tools.store.repository.read(answer_id)
    if previous is not None:
        if previous.value.provenance.get("publication_digest") != publication_digest:
            raise ValueError("settled research projection changed its original publication inputs")
        if tools.store.exists(note_id) and publisher is None:
            tools.synthesis_id = note_id
            if note_id not in tools.created_ids:
                tools.created_ids.append(note_id)
            return note_id
    note = KnowledgeNote.create(
        type="synthesis", title=_synthesis_title(task),
        body=summary if accepted_report else _synthesis_body(summary, tools.ledger), tags=tags,
        sources=sorted(selected_sources), session_id=session_id, project=project,
        open_questions=clean_open_questions(open_questions)[:4],
        id=note_id,
    )
    answer = KnowledgeObject(answer_id, "answer", summary, "model_inference", title=note.title,
        status="active", scope=scope, scope_id=scope_id, references=tuple(dict.fromkeys(references)),
        provenance={"run_id": run_id, "session_id": session_id, "summary_digest": summary_digest,
                    "publication_digest": publication_digest, "publication_schema": 1,
                    "basis": "settled_kernel_projection", "projection": projection,
                    "questions": list(accepted_report.questions if accepted_report else tools.ledger.questions),
                    "question_coverage": list(question_coverage),
                    "coverage_assessment": "model_reported_not_semantically_verified",
                    "accepted_report": accepted_report.to_payload() if accepted_report is not None else None,
                    "task": task, "project": project, "synthesis_id": note_id,
                    "semantic_review": semantic_review})
    if publisher is not None:
        from codey.providers.response_record import json_value

        return publisher({"schema": 1, "note": json_value(note), "commit_id": stable_identity("answer-commit", run_id, projection),
                          "mutations": [{"value": answer.to_dict(), "expected_revision": 0}]})
    try:
        tools.store.publish_note(note, stable_identity("answer-commit", run_id, projection),
                                 [KnowledgeMutation(answer)], changes=tools.changes)
    except OSError as exc:
        from codey.research.pipeline import ResearchContinuationError

        raise ResearchContinuationError(f"research answer publication failed: {exc}") from exc
    if note_id not in tools.created_ids:
        tools.created_ids.append(note_id)
    tools.synthesis_id = note_id
    if accepted_report is None:
        _link_synthesis(tools, note.id, note_ids, policy)
    on_event(RunEvent.info("saved synthesis", names=note.id))
    return note.id


def _link_synthesis(tools: Any, note_id: str, note_ids: list[str], policy: Any) -> None:
    allows_link = getattr(policy, "allows", None)
    try:
        can_link = bool(allows_link("knowledge.link")) if callable(allows_link) else False
    except Exception:
        can_link = False
    if can_link:
        for related_id in note_ids:
            if related_id and related_id != note_id:
                try:
                    tools.store.link(note_id, related_id, "derives", changes=tools.changes)
                except OSError:
                    break


def _publication_proof(outcome: Any, compiled: Any, history: Any, synthesis_id: str, publish: bool) -> Any:
    if compiled is None or not publish:
        return outcome.proof
    from codey.operations.research_publication import published_answer_version
    from codey.research.answer_report import AnswerReport
    from codey.research.report_projection import published_completion

    version = published_answer_version(history, synthesis_id) if isinstance(compiled, AnswerReport) else None
    return published_completion(outcome.proof, compiled, synthesis_id=synthesis_id, result_ref=history.state().work_ref,
                                answer_version=version)


def run_research_iteration(
    deps: Any,
    *,
    provider: Any,
    session_id: str,
    project: str,
    task: str,
    max_turns: int,
    on_event: Callable[[RunEvent], None],
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
    task_policy: Any = None,
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
    session = TaskSession(
        policy=policy, task_kind="research", project=project,
        max_turns=max_turns, task_text=task,
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
    active_provider = provider
    admitted = None
    mutations = getattr(deps, "runtime_mutations", None)
    if mutations is not None and session_id and run_id:
        from codey.operations.task_effects import KernelEffectSink, admitted_durable

        admitted = admitted_durable(getattr(deps, "state", None), run_id)
        intent_sink = KernelEffectSink(
            mutations, session_id=session_id, run_id=run_id,
            provider_id=provider_id, phase="research",
            managed_outputs=getattr(deps, "managed_outputs", None),
            durable=admitted,
        )
    from codey.operations.research_continuation import ResearchContinuation

    history = ResearchContinuation(mutations, getattr(deps, "managed_outputs", None),
                                   session_id=session_id, run_id=run_id)
    prerequisites = history.prerequisite_facts(before_attempt=writer_attempt, policy=policy)
    session.handoff += prerequisites.handoff
    if ((stop_flag is not None and stop_flag.is_set())
            or history.durable.task(run_id).state.get("cancel_requested")):
        from codey.operations.research_publication import settle_cancelled_publications
        from codey.runtime.core.cancellation import TaskCancelled

        if settle_cancelled_publications(history, tools.store):
            raise TaskCancelled("research publication settled after cancellation")
    position = history.state()
    if (position.writer_attempt < writer_attempt
            or (position.writer_attempt == writer_attempt and position.leaf in {"accepted", "writer_running"}
                and not position.logical_turn)):
        provider.new_chat()
    topic_source = source_from_value(
        key="research_topic_continuity",
        text=topic_continuity_context,
        budget=900,
        scope=request_scope(project, session_id),
        capability_id="research_topic_continuity",
    )
    outcome = history.kernel(
        session,
        invocation=invocation,
        request=KernelRunRequestBuilder(
                provider=active_provider,
                run_id=run_id,
                effect_scope=f"research:{writer_attempt}",
                provider_id=provider_id,
                user_task=task,
                stagnant_turns=5,
                context_scope=request_scope(project, session_id),
                ghost_sources=(topic_source,) if topic_source is not None else (),
                admit_hints=ghost_control_enabled(
                    deps.state, "admit_hints", project=project, session_id=session_id,
                ),
                stop_flag=stop_flag,
                task_guidance=task_guidance_for_policy(session.policy, task_kind=session.task_kind),
            ).build(execution=KernelExecutionDeps(
                intent_sink=intent_sink,
                project_path=Path(project) if project else None,
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
        if compiled is not None and compiled.text != outcome.summary:
            raise ValueError("settled report does not match accepted delivery")
        # Deterministic projection of the settled driver output; the stored
        # digest rejects changes to that original output on replay.
        from codey.operations.research_publication import publish_synthesis as publish_answer


        synthesis_id = publish_answer(history,
            tools, task, outcome.summary, session_id=session_id,
            project=project, run_id=run_id, on_event=on_event,
            open_questions=compiled.open_questions if compiled else session.last_done_args.get("open_questions", ()),
            knowledge_refs=[*_proposal_knowledge_refs(session.last_done_args), *(compiled.knowledge_refs if compiled else ())],
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
    result = ResearchRunResult(
        question=task, summary=outcome.summary, stop_reason=outcome.stop_reason,
        turns=outcome.turns,
        queries=[item.query for item in ledger.searches],
        search_results=ledger.search_results_payload(),
        opened_sources=ledger.opened_sources_payload(),
        coverage={**ledger.coverage_payload(),
                  "question_answers": list(compiled.coverage) if outcome.completed and policy.strict_research and compiled else []},
        citation_map=[dict(row) for row in compiled.source_versions] if compiled else quality.citation_payload() if quality is not None else [],
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
