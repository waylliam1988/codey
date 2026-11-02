"""Prepare exact source/evidence mutations for the original answer publication."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from codey.knowledge.archive import stable_identity
from codey.knowledge.model import KnowledgeMutation, KnowledgeObject, KnowledgeReference
from codey.research.answer_contract import digest
from codey.research.knowledge_updates import evidence_labels
from codey.research.passages import passage_ref


def archive_scope(store: Any, *, project: str, session_id: str,
                  versions: Any = ()) -> tuple[str, str]:
    """Inherit restricted material; otherwise retain research for its owner.

    A session is provenance, not the default owner of durable research. Explicit
    session-scoped material stays private; global material stays globally readable.
    """
    from codey.knowledge.recall import KnowledgeAccess

    restricted = set()
    for version in versions:
        if version is None or not KnowledgeAccess(project, session_id, store.owner_id).allows(version):
            raise ValueError("selected stored material is missing or inaccessible")
        if version.value.scope != "global":
            restricted.add((version.value.scope, version.value.scope_id))
    if len(restricted) > 1:
        raise ValueError("selected material combines incompatible restricted scopes")
    return next(iter(restricted)) if restricted else (
        ("project", project) if project else ("personal", store.owner_id))


def prepare_evidence(submission: dict[str, Any], ledger: Any, store: Any, *, project: str,
                     session_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """No writes. Select only inline citations and explicitly authored updates.

    Use each immutable ReadPassage, never the ledger's latest view of its URL.
    Whole answers use evidence directly; no model claims are manufactured here.
    """
    selected = {row["label"]: row["passage_ref"] for row in submission["citations"]}
    aliases = dict(submission["scope"]["aliases"])
    for label in evidence_labels(submission.get("knowledge_updates", [])):
        selected.setdefault(label, aliases[label])
    versions = [store.repository.read(*pin) for ref in selected.values()
                if (passage := ledger.read_passages.get(ref)) is not None
                for pin in (passage.source_version, passage.evidence_version) if pin is not None]
    scope, owner = archive_scope(store, project=project, session_id=session_id, versions=versions)
    mutations: dict[str, KnowledgeMutation] = {}
    sources: dict[tuple[str, int], dict[str, Any]] = {}
    bindings = []

    def pinned(value: KnowledgeObject) -> KnowledgeReference:
        old = store.repository.read(value.id)
        if old is not None:
            if value.kind == "source":
                value = replace(value, provenance={**value.provenance,
                    "retrieved_at": old.value.provenance.get("retrieved_at")})
            if old.value != value:
                raise ValueError("selected immutable evidence differs from its stored version")
            return KnowledgeReference(value.id, old.revision, "uses")
        mutations[value.id] = KnowledgeMutation(value)
        return KnowledgeReference(value.id, 1, "uses")

    for label, ref in selected.items():
        passage = ledger.read_passages.get(ref)
        if passage is None or ref != passage_ref(passage.source.final_url, passage.source.text_hash, passage.start, passage.end,
                source_version=passage.source_version, evidence_version=passage.evidence_version):
            raise ValueError("selected evidence lost its original read passage")
        source = passage.source
        source_id = stable_identity("source", scope, owner, source.final_url, source.text_hash)
        value = KnowledgeObject(source_id, "source", passage.source_text, "source", title=source.title,
            status="active", scope=scope, scope_id=owner,
            provenance={"url": source.final_url, "content_hash": source.text_hash, "retrieved_at": source.retrieved_at,
                        "quality": source.quality.to_dict(), "independence": "unknown",
                        "content_kind": source.content_kind, "truncated": source.truncated,
                        "pages": [{"number": number, "text": text} for number, text in passage.pages]})
        if passage.source_version:
            stored = store.repository.read(*passage.source_version)
            if (stored is None or stored.value.kind != "source" or stored.value.content != passage.source_text
                    or stored.value.provenance.get("url") != source.final_url):
                raise ValueError("read source differs from its pinned knowledge version")
            source_id = stored.value.id
            source_ref = KnowledgeReference(source_id, stored.revision, "uses")
        else:
            source_ref = pinned(value)
        old = store.repository.read(source_id, source_ref.revision)
        sources[source_id, source_ref.revision] = {"id": source_id, "revision": source_ref.revision, "url": source.final_url,
            "title": source.title, "content_hash": source.text_hash,
            "retrieved_at": old.value.provenance.get("retrieved_at", source.retrieved_at) if old else source.retrieved_at,
            "quality": source.quality.to_dict()}
        locator = "passage:" + ref
        evidence_id = stable_identity("evidence", source_id, passage.excerpt, str(passage.page), locator)
        evidence = KnowledgeObject(evidence_id, "evidence", passage.excerpt, "observed_result", title=source.title,
            status="active", scope=scope, scope_id=owner,
            references=(replace(source_ref, relation="from_source"),),
            provenance={"page": passage.page, "locator": locator, "start": passage.start, "end": passage.end})
        if passage.evidence_version:
            stored = store.repository.read(*passage.evidence_version)
            if (stored is None or stored.value.kind != "evidence" or stored.value.content != passage.excerpt
                    or replace(source_ref, relation="from_source") not in stored.value.references):
                raise ValueError("read evidence differs from its pinned knowledge version")
            evidence_id = stored.value.id
            evidence_ref = KnowledgeReference(evidence_id, stored.revision, "uses")
        else:
            evidence_ref = pinned(evidence)
        bindings.append({"label": label, "passage_ref": ref, "evidence_id": evidence_id,
            "evidence_revision": evidence_ref.revision, "source_id": source_id, "source_revision": source_ref.revision,
            "excerpt": passage.excerpt, "excerpt_digest": digest(passage.excerpt), "page": passage.page,
            "locator": locator, "start": passage.start, "end": passage.end, "relation": "uses"})
    if len(mutations) >= 128:
        raise ValueError("selected evidence exceeds the original publication transaction budget")
    return ([{"value": row.value.to_dict(), "expected_revision": row.expected_revision} for row in mutations.values()],
            list(sources.values()), bindings)


def prepared_scope(mutations: Any, evidence: Any, store: Any, *, project: str, session_id: str) -> tuple[str, str]:
    from codey.knowledge.model import KnowledgeVersion

    pending = {row["value"]["id"]: KnowledgeObject.from_dict(row["value"]) for row in mutations}
    versions = [KnowledgeVersion(pending[row["evidence_id"]], row["evidence_revision"], "", "")
                if row["evidence_id"] in pending else store.repository.read(row["evidence_id"], row["evidence_revision"])
                for row in evidence]
    return archive_scope(store, project=project, session_id=session_id, versions=versions)


def publication_command(report: Any, *, task: str, run_id: str, projection: str,
                        note_id: str, session_id: str, project: str, store: Any) -> dict[str, Any]:
    """The original publication command owns all writes, including its one answer."""
    from dataclasses import asdict

    from codey.knowledge.note import KnowledgeNote
    from codey.research.answer_report import AnswerReport
    from codey.research.synthesis import synthesis_title

    report = AnswerReport.from_payload(report.to_payload())
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
    answer_id = stable_identity("answer", run_id, projection)
    answer = KnowledgeObject(answer_id, "answer", report.text, "model_inference", title=note.title,
        status="active", scope=scope, scope_id=owner, references=references,
        provenance={"run_id": run_id, "session_id": session_id, "project": project, "task": task,
            "synthesis_id": note_id, "basis": "settled_whole_answer_submission", "publication_schema": 2,
            "projection": projection, "answer_state": report.state, "semantic_review": None,
            "submission_digest": report.proposal_digest, "compilation_digest": report.to_payload()["compilation_digest"],
            "citation_bindings": list(report.citation_bindings), "evidence_bindings": list(report.evidence_bindings),
            "source_versions": list(report.source_versions), "attempt_refs": list(report.attempt_refs),
            "selection_receipts": list(report.selection_receipts)})
    return {"schema": 1, "note": asdict(note), "commit_id": stable_identity("answer-commit", run_id, projection),
            "mutations": [*report.knowledge_mutations, {"value": answer.to_dict(), "expected_revision": 0}]}
