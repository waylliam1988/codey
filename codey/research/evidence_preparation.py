from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import TYPE_CHECKING

from codey.knowledge.archive import stable_identity
from codey.knowledge.model import KnowledgeMutation, KnowledgeObject, KnowledgeReference, KnowledgeVersion
from codey.knowledge.store import KnowledgeStore
from codey.research.answer_contract import digest
from codey.research.answer_values import AnswerSubmission
from codey.research.knowledge_update_types import MutationPayload
from codey.research.knowledge_updates import evidence_labels
from codey.research.ledger import ResearchLedger
from codey.research.passages import passage_ref
from codey.research.report_values import (
    EvidenceBinding,
    SourceVersion,
    evidence_binding,
    mutation_payload,
    source_version,
)
from codey.research.staging import StagedKnowledgeStore
from codey.types.json_value import canonical_json_object

if TYPE_CHECKING:
    from codey.research.report_snapshot import ReportEvidence, ReportMutation


def archive_scope(store: KnowledgeStore | StagedKnowledgeStore, *, project: str, session_id: str,
                  versions: Sequence[KnowledgeVersion | None] = ()) -> tuple[str, str]:
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


def prepare_evidence(submission: AnswerSubmission, ledger: ResearchLedger, store: KnowledgeStore | StagedKnowledgeStore, *, project: str,
                     session_id: str) -> tuple[list[MutationPayload], list[SourceVersion], list[EvidenceBinding]]:
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
    sources: dict[tuple[str, int], SourceVersion] = {}
    bindings: list[EvidenceBinding] = []

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
        sources[source_id, source_ref.revision] = source_version({"id": source_id, "revision": source_ref.revision, "url": source.final_url,
            "title": source.title, "content_hash": source.text_hash,
            "retrieved_at": old.value.provenance.get("retrieved_at", source.retrieved_at) if old else source.retrieved_at,
            "quality": source.quality.to_dict()})
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
    return ([mutation_payload({"value": row.value.to_dict(), "expected_revision": row.expected_revision}) for row in mutations.values()],
            [source_version(row) for row in sources.values()], [evidence_binding(row) for row in bindings])


def prepared_scope(mutations: Sequence[MutationPayload | ReportMutation], evidence: Sequence[EvidenceBinding | ReportEvidence], store: KnowledgeStore | StagedKnowledgeStore, *, project: str, session_id: str) -> tuple[str, str]:
    from codey.knowledge.model import KnowledgeVersion

    pending = {value.id: value for row in mutations for value in (KnowledgeObject.from_dict(canonical_json_object(row["value"])),)}
    versions = [KnowledgeVersion(pending[row["evidence_id"]], row["evidence_revision"], "", "")
                if row["evidence_id"] in pending else store.repository.read(row["evidence_id"], row["evidence_revision"])
                for row in evidence]
    return archive_scope(store, project=project, session_id=session_id, versions=versions)
