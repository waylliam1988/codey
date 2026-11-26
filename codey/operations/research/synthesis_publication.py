from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping

from codey.knowledge.changes import KnowledgeChanges
from codey.knowledge.model import KnowledgeReference
from codey.knowledge.store import KnowledgeStore
from codey.policies.task_policy import TaskPolicy
from codey.research.answer_report import AnswerReport
from codey.research.staging import StagedKnowledgeStore
from codey.research.tools import ResearchTools
from codey.runtime.observe.events import RunEvent


def _stable_synthesis_id(run_id: str) -> str:
    """同一 run 的合成笔记恒用同一身份：写入成功后交付失败，重试复用原 ID。"""
    import hashlib

    if not run_id:
        raise ValueError("synthesis requires a run identity")
    return "synthesis-" + hashlib.sha256(run_id.encode("utf-8")).hexdigest()


def _selected_note_sources(store: KnowledgeStore | StagedKnowledgeStore, note_ids: list[str]) -> set[str]:
    sources: set[str] = set()
    for identity in note_ids:
        note = store.read_note(identity)
        if note is not None:
            sources.update(note.sources)
    return sources


def _persist_synthesis(tools: ResearchTools, task: str, summary: str, *, session_id: str,
                       project: str, run_id: str,
                       on_event: Callable[[RunEvent], None], open_questions: Iterable[object] = (),
                       policy: TaskPolicy | None, projection: str = "initial", knowledge_refs: Iterable[object] = (),
                       finding_ids: tuple[str, ...] = (), question_coverage: Iterable[object] = (), accepted_report: AnswerReport | None = None,
                       semantic_review: dict[str, object] | None = None, publisher: Callable[[dict[str, object]], str] | None = None) -> str:
    """Persist one synthesis note under the parent task policy (required).

    ``policy=None`` denies like any non-allowing policy (fail-closed). The
    note id is stable per run: a retry after a successful write reuses the
    original id via store upsert instead of creating a duplicate note.
    """
    if not summary or tools.store is None or tools.changes is None:
        return ""
    try:
        permitted = policy is not None and policy.allows("knowledge.write")
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
        return publisher(dict[str, object](publication_command(accepted_report, task=task, run_id=run_id, projection=projection,
            note_id=note_id, session_id=session_id, project=project, store=tools.store)))
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

    note_ids = list(finding_ids)
    selected_sources = _selected_note_sources(tools.store, note_ids)
    tags = ["research", *([f"session:{session_id}"] if session_id else [])]
    tags.extend(_run_concept_tags(tools.store, note_ids))
    from codey.knowledge.answer_refs import answer_references, answer_scope

    references = list(answer_references(knowledge_refs, tools.store, project=project, session_id=session_id))
    scope, scope_id = answer_scope(references, tools.store, project=project, session_id=session_id)
    publication_payload: dict[str, object] = {"schema": 1, "task": task, "summary": summary,
        "references": [{"id": ref.target, "revision": ref.revision} for ref in references],
        "findings": list(finding_ids), "coverage": list(question_coverage), "open_questions": list(open_questions),
        "scope": [scope, scope_id], "session_id": session_id, "project": project, "sources": sorted(selected_sources),
        "report": None,
        "semantic_review": semantic_review}
    publication_digest = hashlib.sha256(canonical_json(publication_payload).encode()).hexdigest()
    previous = tools.store.repository.read(answer_id)
    if previous is not None:
        previous_provenance: Mapping[str, object] = previous.value.provenance
        if previous_provenance.get("publication_digest") != publication_digest:
            raise ValueError("settled research projection changed its original publication inputs")
        if tools.store.exists(note_id) and publisher is None:
            tools.synthesis_id = note_id
            if note_id not in tools.created_ids:
                tools.created_ids.append(note_id)
            return note_id
    note_sources: list[str] = sorted(selected_sources)
    note = KnowledgeNote.create(
        type="synthesis", title=_synthesis_title(task),
        body=_synthesis_body(summary, tools.ledger), tags=tags,
        sources=note_sources, session_id=session_id, project=project,
        open_questions=clean_open_questions(open_questions)[:4],
        id=note_id,
    )
    unique_references: dict[KnowledgeReference, None] = dict.fromkeys(references)
    provenance: dict[str, object] = {"run_id": run_id, "session_id": session_id, "summary_digest": summary_digest,
                    "publication_digest": publication_digest, "publication_schema": 1,
                    "basis": "settled_kernel_projection", "projection": projection,
                    "questions": list(tools.ledger.questions),
                    "question_coverage": list(question_coverage),
                    "coverage_assessment": "model_reported_not_semantically_verified",
                    "accepted_report": None,
                    "task": task, "project": project, "synthesis_id": note_id,
                    "semantic_review": semantic_review}
    answer = KnowledgeObject(answer_id, "answer", summary, "model_inference", title=note.title,
        status="active", scope=scope, scope_id=scope_id, references=tuple(unique_references), provenance=provenance)
    if publisher is not None:
        from codey.providers.response_record import json_value

        answer_payload: dict[str, object] = answer.to_dict()
        return publisher({"schema": 1, "note": json_value(note), "commit_id": stable_identity("answer-commit", run_id, projection),
                          "mutations": [{"value": answer_payload, "expected_revision": 0}]})
    try:
        tools.store.publish_note(note, stable_identity("answer-commit", run_id, projection),
                                 [KnowledgeMutation(answer)], changes=tools.changes if isinstance(tools.changes, KnowledgeChanges) else None)
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


def _link_synthesis(tools: ResearchTools, note_id: str, note_ids: list[str], policy: TaskPolicy | None) -> None:
    try:
        can_link = policy is not None and policy.allows("knowledge.link")
    except Exception:
        can_link = False
    if can_link and tools.store is not None:
        for related_id in note_ids:
            if related_id and related_id != note_id:
                try:
                    tools.store.link(note_id, related_id, "derives", changes=tools.changes if isinstance(tools.changes, KnowledgeChanges) else None)
                except OSError:
                    break
