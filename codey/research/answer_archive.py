"""Prepare exact source/evidence mutations for the original answer publication."""

from __future__ import annotations

from typing import TYPE_CHECKING

from codey.knowledge.store import KnowledgeStore
from codey.research.evidence_preparation import archive_scope as archive_scope
from codey.research.evidence_preparation import prepare_evidence as prepare_evidence
from codey.research.evidence_preparation import prepared_scope as prepared_scope
from codey.research.staging import StagedKnowledgeStore
from codey.types.json_value import JsonObject, canonical_json_object

if TYPE_CHECKING:
    from codey.research.answer_report import AnswerReport

from codey.knowledge.archive import stable_identity
from codey.knowledge.model import KnowledgeObject, KnowledgeReference
from codey.research.answer_contract import digest


def publication_command(report: AnswerReport, *, task: str, run_id: str, projection: str,
                        note_id: str, session_id: str, project: str, store: KnowledgeStore | StagedKnowledgeStore) -> JsonObject:
    """The original publication command owns all writes, including its one answer."""
    from dataclasses import asdict

    from codey.knowledge.note import KnowledgeNote
    from codey.research.synthesis import synthesis_title

    if report.task_digest != digest(task):
        raise ValueError("publication task differs from the submitted answer")
    scope, owner = prepared_scope(report.knowledge_mutations, report.evidence_bindings, store,
                                  project=project, session_id=session_id)
    if report.knowledge_batch is not None and (scope, owner) != (
            report.knowledge_batch["scope"], report.knowledge_batch["scope_id"]):
        raise ValueError("knowledge batch scope differs from its selected material")
    if report.knowledge_batch is not None and report.knowledge_batch["session_id"] != session_id:
        raise ValueError("knowledge batch belongs to another session")
    references = tuple(KnowledgeReference(row["id"], row["revision"], "uses") for row in report.knowledge_refs)
    note = KnowledgeNote.create(type="synthesis", title=synthesis_title(task), body=report.text,
        tags=["research", *([f"session:{session_id}"] if session_id else [])],
        sources=sorted({row["url"] for row in report.source_versions}), session_id=session_id, project=project,
        open_questions=list(report.open_questions), id=note_id)
    payload = report.to_payload()
    answer_id = stable_identity("answer", run_id, projection)
    answer = KnowledgeObject(answer_id, "answer", report.text, "model_inference", title=note.title,
        status="active", scope=scope, scope_id=owner, references=references,
        provenance={"run_id": run_id, "session_id": session_id, "project": project, "task": task,
            "synthesis_id": note_id, "basis": "settled_whole_answer_submission", "publication_schema": 2,
            "projection": projection, "answer_state": report.state, "semantic_review": None,
            "submission_digest": report.proposal_digest, "compilation_digest": payload["compilation_digest"],
            "citation_bindings": payload["citation_bindings"], "evidence_bindings": payload["evidence_bindings"],
            "source_versions": payload["source_versions"], "attempt_refs": list(report.attempt_refs),
            "selection_receipts": list(report.selection_receipts)})
    return canonical_json_object({"schema": 1, "note": asdict(note), "commit_id": stable_identity("answer-commit", run_id, projection),
            "mutations": [*report.knowledge_mutations, {"value": answer.to_dict(), "expected_revision": 0}]})
