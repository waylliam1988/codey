"""Promote verified tool observations, never report summaries, into knowledge."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import Any

from codey.knowledge.model import KnowledgeMutation, KnowledgeObject, KnowledgeReference, canonical_json
from codey.knowledge.note import KnowledgeNote
from codey.knowledge.repository import KnowledgeRepository
from codey.research.ledger import EvidencePreparation, ResearchLedger


def stable_identity(kind: str, *parts: str) -> str:
    return kind + "-" + hashlib.sha256(canonical_json(parts).encode()).hexdigest()


def note_knowledge(
    note: KnowledgeNote, ledger: ResearchLedger, evidence: EvidencePreparation,
    repository: KnowledgeRepository, *, conditions: dict[str, dict[str, str]] | None = None,
) -> tuple[str, list[KnowledgeMutation]]:
    """Build a bounded commit from full verified excerpts, not clipped projections.

    Source identity includes the retrieved content hash. A changed page therefore
    never silently changes what an old evidence fragment meant.
    """
    objects: dict[str, KnowledgeObject] = {}
    scope, scope_id = ("project", note.project) if note.project else ("global", "")

    def add(value: KnowledgeObject) -> KnowledgeReference:
        objects[value.id] = value
        old = repository.read(value.id)
        revision = old.revision if old and old.value == value else (old.revision + 1 if old else 1)
        return KnowledgeReference(value.id, revision, "about")

    entities: list[KnowledgeReference] = []
    for tag in dict.fromkeys(note.tags):
        if tag.startswith(("session:", "project:")) or tag == "research":
            continue
        entity = KnowledgeObject(stable_identity("entity", scope, scope_id, tag.casefold()), "entity", tag, "source",
                                 title=tag, scope=scope, scope_id=scope_id, status="active")
        entities.append(add(entity))
    for item in evidence.items:
        source = ledger.source_record_for_url(item.source_url)
        if source is None:
            continue  # Only opened and verified observations may become evidence.
        full_text = ledger.source_text_for_url(item.source_url)
        source_id = stable_identity("source", scope, scope_id, source.final_url, source.text_hash)
        source_value = KnowledgeObject(source_id, "source", full_text, "source", title=source.title,
            status="active", scope=scope, scope_id=scope_id,
            provenance={"url": source.final_url, "content_hash": source.text_hash, "retrieved_at": source.retrieved_at,
                        "quality": source.quality.to_dict(), "independence": "unknown",
                        "content_kind": source.content_kind, "truncated": source.truncated})
        source_ref = replace(add(source_value), relation="from_source")
        evidence_id = stable_identity("evidence", source_id, item.excerpt, str(item.page), item.locator)
        fragment = KnowledgeObject(evidence_id, "evidence", item.excerpt, "observed_result", title=item.claim,
            status="active", scope=scope, scope_id=scope_id, references=(source_ref,),
            provenance={"page": item.page, "locator": item.locator, "note_id": note.id})
        fragment_ref = add(fragment)
        relation = {"supports": "supported_by", "contradicts": "refuted_by", "limits": "limited_by"}.get(
            item.stance, "limited_by")
        claim_id = stable_identity("claim", note.id, item.claim)
        claim = KnowledgeObject(claim_id, "claim", item.claim, "author_interpretation", title=item.claim,
            status="active" if relation == "supported_by" else "candidate", scope=scope, scope_id=scope_id,
            aliases=tuple(note.aliases), conditions=conditions or {},
            provenance={"note_id": note.id, "session_id": note.session_id,
                        "interpretation": "model_extraction_requires_review"},
            references=(replace(fragment_ref, relation=relation), *entities))
        # Multiple excerpts about one claim keep their individual stances.
        previous = objects.get(claim_id)
        if previous:
            claim = replace(claim, references=tuple(dict.fromkeys((*previous.references, *claim.references))),
                            status="active" if previous.status == "active" or claim.status == "active" else "candidate")
        add(claim)
    if not evidence.items and note.type in {"hypothesis", "question", "conclusion", "decision", "verification"}:
        # Saving a provisional interpretation is useful; lack of evidence never
        # turns it into an established result, even if the note says "conclusion".
        add(KnowledgeObject(stable_identity("claim", note.id), "question" if note.type == "question" else "claim",
            note.body, "model_inference", title=note.title, scope=scope, scope_id=scope_id,
            conditions=conditions or {}, aliases=tuple(note.aliases), references=tuple(entities),
            provenance={"note_id": note.id, "session_id": note.session_id, "basis": "unverified_note"}))
    mutations: list[KnowledgeMutation] = []
    for value in objects.values():
        old = repository.read(value.id)
        if old is None or old.value != value:
            mutations.append(KnowledgeMutation(value, old.revision if old else 0))
    digest_data: list[dict[str, Any]] = [{"value": m.value.to_dict(), "expected_revision": m.expected_revision} for m in mutations]
    return stable_identity("archive", note.id, canonical_json(digest_data)), mutations
