"""Unified evidence reference semantics for Codey's research facts.

Evidence Runtime is a deterministic projection layer over facts that already
exist elsewhere: ResearchRecord object ids, proof-review refs, AnalysisRun
records, and Artifact lineage versions. It owns exactly two things:

1. The shared meaning of a ``<kind>:<id>`` runtime ref (one validator instead
   of per-module copies of the same regex).
2. A bounded read-model snapshot of one research record's evidence graph.

It is not a lifecycle owner: it never performs I/O, never calls models, never
fetches sources, and never imports runtime layers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Mapping

from codey.refs import digest_ref as _digest_ref
from codey.refs import identifier as _identifier
from codey.research.object_model import (
    MAX_RECORD_ASSUMPTIONS,
    MAX_RECORD_CLAIMS,
    MAX_RECORD_EVIDENCE,
    MAX_RECORD_RELATIONS,
    MAX_RECORD_SOURCES,
    ResearchRecord,
)
from codey.research.shape import generated_ref as _generated_ref


_HEX16_RE = re.compile(r"^[0-9a-f]{16}$")
_BOUNDED_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_MAX_REF_CHARS = 120

# Generated content-addressed refs share one value shape: <kind>:<16 hex>.
_GENERATED_REF_KINDS = frozenset({
    "source",
    "evidence",
    "claim",
    "assumption",
    "relation",
    "research_record",
    "research_proof",
    "research_plan",
    "analysis_run",
    "artifact",
    "artifact_version",
    "review_finding",
    "planner_gap",
})
# Runtime-scoped ids keep their bounded token shape instead of being hashed.
_BOUNDED_REF_KINDS = frozenset({"run"})
RUNTIME_REF_KINDS = _GENERATED_REF_KINDS | _BOUNDED_REF_KINDS

# Snapshot caps mirror the object model's own record caps so a typed record
# projects losslessly while adversarial mappings still stay bounded.
MAX_SNAPSHOT_ANALYSIS_RUNS = 8
MAX_SNAPSHOT_ARTIFACT_VERSIONS = 16
_ANSWER_STATUSES = frozenset({
    "answered",
    "partial",
    "insufficient_evidence",
    "not_answered",
})


def runtime_ref_kinds() -> tuple[str, ...]:
    return tuple(sorted(RUNTIME_REF_KINDS))


def runtime_ref_kind(value: object) -> str:
    """Return the validated ref kind, or "" when the value is not a ref."""

    text = str(value or "").strip()
    if len(text) > _MAX_REF_CHARS or ":" not in text:
        return ""
    prefix, _, suffix = text.partition(":")
    if prefix in _GENERATED_REF_KINDS:
        return prefix if _HEX16_RE.fullmatch(suffix) else ""
    if prefix in _BOUNDED_REF_KINDS:
        return prefix if _BOUNDED_ID_RE.fullmatch(suffix) else ""
    return ""


def is_valid_runtime_ref(value: object, *, kinds: Iterable[str] | None = None) -> bool:
    """Validate one runtime ref against the shared kind allowlist.

    ``kinds=None`` accepts every known kind; passing an explicit subset keeps
    narrow boundaries narrow (e.g. artifact lineage only derives from
    source/evidence/analysis_run/run facts).
    """

    kind = runtime_ref_kind(value)
    if not kind:
        return False
    if kinds is None:
        return True
    allowed = frozenset(kinds)
    return bool(allowed) and kind in allowed


def normalize_runtime_ref(value: object, *, kind: str | None = None) -> str:
    """Return the ref when valid (optionally restricted to one kind), else ""."""

    text = str(value or "").strip()
    kinds = (kind,) if kind else None
    return text if is_valid_runtime_ref(text, kinds=kinds) else ""


def bounded_runtime_refs(
    values: Iterable[object],
    *,
    limit: int = 12,
) -> tuple[str, ...]:
    """Normalize, dedupe, and cap a sequence of runtime refs."""

    refs: list[str] = []
    for value in values or ():
        ref = normalize_runtime_ref(value)
        if not ref or ref in refs:
            continue
        refs.append(ref)
        if len(refs) >= max(0, int(limit)):
            break
    return tuple(refs)


@dataclass(frozen=True)
class EvidenceRuntimeSnapshot:
    """Bounded read model over one research record's evidence graph.

    Every field is either a validated ref, a digest, an allow-listed status,
    or a tuple of validated refs. There is no raw text by design.
    """

    record_ref: str = ""
    record_digest: str = ""
    answer_status: str = "not_answered"
    proof_ref: str = ""
    question_digest: str = ""
    source_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    claim_refs: tuple[str, ...] = ()
    assumption_refs: tuple[str, ...] = ()
    relation_refs: tuple[str, ...] = ()
    analysis_run_refs: tuple[str, ...] = ()
    artifact_version_refs: tuple[str, ...] = ()

    def counts(self) -> dict[str, int]:
        return {
            "sources": len(self.source_refs),
            "evidence": len(self.evidence_refs),
            "claims": len(self.claim_refs),
            "assumptions": len(self.assumption_refs),
            "relations": len(self.relation_refs),
            "analysis_runs": len(self.analysis_run_refs),
            "artifact_versions": len(self.artifact_version_refs),
        }

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "record_ref": self.record_ref,
            "record_digest": self.record_digest,
            "answer_status": self.answer_status,
            "source_refs": list(self.source_refs),
            "evidence_refs": list(self.evidence_refs),
            "claim_refs": list(self.claim_refs),
            "assumption_refs": list(self.assumption_refs),
            "relation_refs": list(self.relation_refs),
            "analysis_run_refs": list(self.analysis_run_refs),
            "artifact_version_refs": list(self.artifact_version_refs),
            "counts": self.counts(),
        }
        if self.proof_ref:
            payload["proof_ref"] = self.proof_ref
        if self.question_digest:
            payload["question_digest"] = self.question_digest
        return payload


def snapshot_from_research_record(
    record: ResearchRecord | Mapping[str, object] | None,
    *,
    proof_review: object | None = None,
    analysis_runs: Iterable[object] = (),
    artifacts: Iterable[object] = (),
) -> EvidenceRuntimeSnapshot | None:
    """Project one research record plus its audit neighbors into a snapshot.

    Returns None when there is no valid research record ref to anchor the
    snapshot, so callers can fail open without projecting half facts.
    """

    payload = _record_payload(record)
    if payload is None:
        return None
    record_ref = _generated_ref(payload.get("record_id"), "research_record")
    if not record_ref:
        return None
    sources = _refs_from_items(payload.get("sources"), "source_id", "source", MAX_RECORD_SOURCES)
    evidence = _refs_from_items(payload.get("evidence"), "evidence_id", "evidence", MAX_RECORD_EVIDENCE)
    claims = _refs_from_items(payload.get("claims"), "claim_id", "claim", MAX_RECORD_CLAIMS)
    assumptions = _refs_from_items(
        payload.get("assumptions"),
        "assumption_id",
        "assumption",
        MAX_RECORD_ASSUMPTIONS,
    )
    relations = _refs_from_items(payload.get("relations"), "relation_id", "relation", MAX_RECORD_RELATIONS)
    run_refs: list[str] = []
    artifact_refs: list[str] = []
    for item in analysis_runs or ():
        ref = normalize_runtime_ref(_field(item, "analysis_run_id"), kind="analysis_run")
        if not ref or ref in run_refs:
            continue
        run_refs.append(ref)
        if len(run_refs) >= MAX_SNAPSHOT_ANALYSIS_RUNS:
            break
    for item in artifacts or ():
        ref = normalize_runtime_ref(_field(item, "version_id"), kind="artifact_version")
        if not ref or ref in artifact_refs:
            continue
        artifact_refs.append(ref)
        if len(artifact_refs) >= MAX_SNAPSHOT_ARTIFACT_VERSIONS:
            break
    answer_status = _identifier(payload.get("answer_status"), 40)
    return EvidenceRuntimeSnapshot(
        record_ref=record_ref,
        record_digest=_digest_ref(payload.get("record_digest")),
        answer_status=answer_status if answer_status in _ANSWER_STATUSES else "not_answered",
        proof_ref=_generated_ref(_field(proof_review, "proof_ref"), "research_proof"),
        question_digest=_digest_ref(_field(proof_review, "question_digest")),
        source_refs=sources,
        evidence_refs=evidence,
        claim_refs=claims,
        assumption_refs=assumptions,
        relation_refs=relations,
        analysis_run_refs=tuple(run_refs),
        artifact_version_refs=tuple(artifact_refs),
    )


def _record_payload(record: ResearchRecord | Mapping[str, object] | None) -> dict[str, object] | None:
    if record is None:
        return None
    if isinstance(record, ResearchRecord):
        try:
            return dict(record.to_jsonable())
        except Exception:
            return None
    if isinstance(record, Mapping):
        return dict(record)
    return None


def _field(item: object, name: str) -> object:
    if isinstance(item, Mapping):
        return item.get(name)
    return getattr(item, name, "")


def _refs_from_items(value: object, id_key: str, kind: str, limit: int) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    refs: list[str] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        ref = normalize_runtime_ref(item.get(id_key), kind=kind)
        if not ref or ref in refs:
            continue
        refs.append(ref)
        if len(refs) >= limit:
            break
    return tuple(refs)


__all__ = [
    "EvidenceRuntimeSnapshot",
    "MAX_SNAPSHOT_ANALYSIS_RUNS",
    "MAX_SNAPSHOT_ARTIFACT_VERSIONS",
    "RUNTIME_REF_KINDS",
    "bounded_runtime_refs",
    "is_valid_runtime_ref",
    "normalize_runtime_ref",
    "runtime_ref_kind",
    "runtime_ref_kinds",
    "snapshot_from_research_record",
]
