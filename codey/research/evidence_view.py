"""Resolve versioned evidence without adding historical actions to a ledger."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from codey.knowledge.model import KnowledgeVersion
from codey.knowledge.recall import KnowledgeAccess
from codey.research.ledger import ResearchLedger
from codey.research.reference import ResearchReference
from codey.research.references import read_version, resolve_version


@dataclass(frozen=True)
class ResolvedEvidence:
    claim: KnowledgeVersion
    evidence: KnowledgeVersion
    source: KnowledgeVersion
    relation: str
    acquisition: str


@dataclass(frozen=True)
class ResolvedReference:
    label: str
    binding: ResearchReference
    version: KnowledgeVersion
    evidence: tuple[ResolvedEvidence, ...]


def resolve_selected_references(labels: tuple[str, ...], ledger: ResearchLedger, store: Any, *,
                                project: str, session_id: str) -> tuple[ResolvedReference, ...]:
    access = KnowledgeAccess(project, session_id, store.owner_id)

    def read(identity: str, revision: int) -> KnowledgeVersion:
        version = read_version(store, identity, revision)
        if version is None or not access.allows(version) or version.value.kind == "answer":
            raise ValueError("missing or inaccessible evidence version, or old answer used as evidence")
        return version

    def visit(version: KnowledgeVersion, path: frozenset[tuple[str, int]], acquisition: str) -> list[ResolvedEvidence]:
        key = version.value.id, version.revision
        if key in path or len(path) >= 12:
            raise ValueError("cyclic or overly deep knowledge premises")
        read(*key)
        found = []
        for ref in version.value.references:
            if ref.relation == "depends_on":
                if version.value.kind != "inference":
                    raise ValueError("only an inference may adopt premises as its evidence basis")
                target = read(ref.target, ref.revision)
                if target.value.kind not in {"claim", "inference", "observation"}:
                    raise ValueError("invalid inference premise kind")
                premise_binding = next((row for row in ledger.references.values()
                    if (row.object_id, row.revision) == (target.value.id, target.revision)), None)
                premise_acquisition = premise_binding.acquisition if premise_binding else "prior_knowledge"
                found.extend(visit(target, path | {key}, premise_acquisition))
            elif ref.relation in {"supported_by", "limited_by", "refuted_by"}:
                if version.value.kind == "inference":
                    raise ValueError("candidate inference cannot masquerade as direct source evidence")
                fragment = read(ref.target, ref.revision)
                if fragment.value.kind != "evidence":
                    raise ValueError("claim evidence reference must target evidence")
                for source_ref in fragment.value.references:
                    if source_ref.relation != "from_source":
                        continue
                    source = read(source_ref.target, source_ref.revision)
                    url = source.value.provenance.get("url", "")
                    if (source.value.kind != "source" or not isinstance(url, str)
                            or not url.startswith(("https://", "http://"))
                            or fragment.value.content not in source.value.content):
                        raise ValueError("evidence fragment is not in the exact original source version")
                    found.append(ResolvedEvidence(version, fragment, source, ref.relation, acquisition))
        return found

    result = []
    for label in labels:
        version = resolve_version(ledger, store, label)
        binding = ledger.references[label]
        evidence = visit(version, frozenset(), binding.acquisition)
        if not evidence:
            raise ValueError(f"[{label}] has no source-backed premise; preserve it as an open hypothesis")
        result.append(ResolvedReference(label, binding, version, tuple(evidence)))
    return tuple(result)


def reference_evidence_preview(ledger: ResearchLedger, store: Any, label: str, *, project: str, session_id: str
                               ) -> dict[str, Any]:
    try:
        resolved = resolve_selected_references((label,), ledger, store, project=project, session_id=session_id)[0]
    except ValueError as exc:
        return {"evidence": [], "evidence_issue": str(exc), "evidence_complete": False}
    evidence = []
    for row in resolved.evidence[:3]:
        evidence.append({"claim_id": row.claim.value.id, "claim_revision": row.claim.revision,
            "evidence_id": row.evidence.value.id, "evidence_revision": row.evidence.revision,
            "source_id": row.source.value.id, "source_revision": row.source.revision,
            "excerpt": row.evidence.value.content[:1200], "excerpt_complete": len(row.evidence.value.content) <= 1200,
            "relation": row.relation, "acquisition": row.acquisition})
    return {"evidence": evidence, "evidence_complete": len(resolved.evidence) <= 3 and
            all(row["excerpt_complete"] for row in evidence),
            "evidence_basis": "premises_only" if resolved.version.value.kind == "inference" else "direct_claim_links"}
