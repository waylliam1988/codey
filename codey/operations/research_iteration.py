"""Research pipeline iteration driven by the shared task turn kernel."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

from codey.operations.task_guidance import task_guidance_for_policy
from codey.operations.task_loop import (
    KernelExecutionDeps,
    KernelObservationDeps,
    KernelRunRequest,
    KernelTransportDeps,
)
from codey.research.pipeline import ResearchIterationRun
from codey.research.run_result import ResearchRunResult


def _stable_synthesis_id(run_id: str) -> str:
    """同一 run 的合成笔记恒用同一身份：写入成功后交付失败，重试复用原 ID。"""
    import hashlib

    if not run_id:
        raise ValueError("synthesis requires a run identity")
    return "synthesis-" + hashlib.sha256(run_id.encode("utf-8")).hexdigest()


def _persist_synthesis(tools: Any, task: str, summary: str, *, session_id: str,
                       project: str, run_id: str,
                       on_event: Callable[[object], None], open_questions: Any = (),
                       policy: Any) -> str:
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
    note_id = _stable_synthesis_id(run_id)
    if tools.store.exists(note_id):
        if note_id not in tools.created_ids:
            tools.created_ids.append(note_id)
        return note_id
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
        id=note_id,
    )
    try:
        tools.store.write_note(note, changes=tools.changes)
    except OSError:
        return ""
    if note_id not in tools.created_ids:
        tools.created_ids.append(note_id)
    can_link = True
    allows_link = getattr(policy, "allows", None)
    try:
        can_link = bool(allows_link("knowledge.link")) if callable(allows_link) else False
    except Exception:
        can_link = False
    if can_link:
        for related_id in note_ids:
            if related_id and related_id != note.id:
                try:
                    tools.store.link(note.id, related_id, "derives", changes=tools.changes)
                except OSError:
                    break
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
    task_policy: Any = None,
    controller_denied: tuple[str, ...] = (),
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
    from codey.operations.provider_session import ObservedProvider
    from codey.runtime.observe.prompt_envelope import FailOpenPromptTrace
    from codey.workspace.context_epoch import context_epoch_id

    def observe_send(name: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        if name not in {"send", "send_turn"} or not args:
            return
        prompt = args[0]
        if (not isinstance(prompt, str) or not topic_continuity_context
                or topic_continuity_context not in prompt or not topic_continuity_payload
                or topic_continuity_payload.get("admitted") is not True):
            return
        FailOpenPromptTrace(trace_recorder).call(
            "record_research_topic_continuity", topic_continuity_payload,
            epoch_id=context_epoch_id(prompt),
        )

    provider = ObservedProvider(provider, observe_send)
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
    intent_sink = None
    active_provider = provider
    mutations = getattr(deps, "runtime_mutations", None)
    if mutations is not None and session_id and run_id:
        from codey.operations.task_effects import KernelEffectSink, KernelRecordedProvider

        mutations.mark_writer_running(session_id, run_id, provider_id=provider_id)
        intent_sink = KernelEffectSink(
            mutations, session_id=session_id, run_id=run_id,
            provider_id=provider_id, phase="research",
            managed_outputs=getattr(deps, "managed_outputs", None),
        )
        active_provider = KernelRecordedProvider(provider, intent_sink)
    outcome = run_task_kernel(
        session,
        request=KernelRunRequest(
            task_guidance=task_guidance_for_policy(session.policy),
            transport=KernelTransportDeps(
                provider=active_provider,
                run_id=run_id,
                effect_scope="research:1",
                provider_id=provider_id,
                user_task=task,
                stop_flag=stop_flag,
            ),
            execution=KernelExecutionDeps(
                project_path=project or None,
                research_tools=tools,
                managed_outputs=getattr(deps, "managed_outputs", None),
                session_id=session_id,
                permission_profile="coding_writer" if policy.allows("project.write") else "research",
            ),
            observation=KernelObservationDeps(
                intent_sink=intent_sink,
                trace_recorder=trace_recorder,
                on_event=on_event,
                completion_context={
                    "run_id": run_id,
                    "question": task,
                    "project": project,
                    "research_ledger": tools.ledger,
                    "source_ids": session.source_ids,
                },
            ),
        ),
    )
    ledger = tools.ledger
    synthesis_id = ""
    if outcome.completed:
        # 幂等恢复：稳定 run 身份使“写入成功、交付失败”后重试复用原笔记，
        # 绝不产生第二条合成（store.exists 命中即直接返回原 ID）。
        synthesis_id = _persist_synthesis(
            tools, task, outcome.summary, session_id=session_id,
            project=project, run_id=run_id, on_event=on_event,
            open_questions=session.last_done_args.get("open_questions", ()),
            policy=policy,
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
        completion_proof=outcome.proof,
    )
    return ResearchIterationRun(result=result, tools=tools)


__all__ = ["run_research_iteration"]
