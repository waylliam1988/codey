"""Bounded refs, claims, and audit context for a Research record.

Projects existing facts without reading raw bodies or authorizing tools.
Offline implementation-impact and handoff rendering live in benchmark tooling.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from codey.research.evidence_runtime import (
    EvidenceRuntimeSnapshot,
)
from codey.research.evidence_runtime import (
    normalize_runtime_ref as _normalize_runtime_ref,
)
from codey.research.guards import generated_ref as _generated_ref
from codey.research.guards import status_token as _status_token
from codey.utils.refs import clip as _clip
from codey.utils.refs import content_digest
from codey.utils.refs import identifier as _identifier

MAX_BRIEF_CLAIMS = 16
MAX_BRIEF_REFS = 24
MAX_BRIEF_TEXT_ITEMS = 6
MAX_OPEN_QUESTIONS = 5
MAX_CLAIM_TEXT_CHARS = 260
MAX_ITEM_TEXT_CHARS = 220
ANSWER_STATUSES = frozenset({
    "answered",
    "partial",
    "insufficient_evidence",
    "not_answered",
})
CLAIM_STATUSES = frozenset({"evidence_backed", "assumption", "unsupported"})


@dataclass(frozen=True)
class ClaimSummary:
    """One bounded claim row: ref, status, and the claim's own text."""

    claim_ref: str
    text: str
    status: str = "unsupported"
    evidence_count: int = 0

    def to_payload(self) -> dict[str, object]:
        return {
            "claim_ref": self.claim_ref,
            "text": _clip(self.text, MAX_CLAIM_TEXT_CHARS),
            "status": _status_token(self.status, CLAIM_STATUSES, default="unsupported"),
            "evidence_count": max(0, (self.evidence_count or 0)),
        }


@dataclass(frozen=True)
class ResearchBriefProjection:
    record_ref: str = ""
    record_digest: str = ""
    answer_status: str = "not_answered"
    profile_id: str = "general"
    claim_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    assumption_refs: tuple[str, ...] = ()
    analysis_run_refs: tuple[str, ...] = ()
    artifact_version_refs: tuple[str, ...] = ()
    source_count: int = 0
    proof_review_refs: tuple[str, ...] = ()
    planner_gap_refs: tuple[str, ...] = ()
    review_finding_refs: tuple[str, ...] = ()
    contract_refs: tuple[str, ...] = ()
    claims: tuple[ClaimSummary, ...] = ()
    open_questions: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def counts(self) -> dict[str, int]:
        return {
            "claims": len(self.claim_refs),
            "evidence": len(self.evidence_refs),
            "assumptions": len(self.assumption_refs),
            "sources": self.source_count,
            "analysis_runs": len(self.analysis_run_refs),
            "artifact_versions": len(self.artifact_version_refs),
            "proof_reviews": len(self.proof_review_refs),
            "planner_gaps": len(self.planner_gap_refs),
            "review_findings": len(self.review_finding_refs),
        }

    def supported_claims(self) -> tuple[ClaimSummary, ...]:
        return tuple(item for item in self.claims if item.status == "evidence_backed")

    def uncertain_claims(self) -> tuple[ClaimSummary, ...]:
        return tuple(
            item for item in self.claims if item.status in {"assumption", "unsupported"}
        )

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "record_ref": self.record_ref,
            "record_digest": self.record_digest,
            "answer_status": _status_token(self.answer_status, ANSWER_STATUSES, default="not_answered"),
            "profile_id": _identifier(self.profile_id, 80) or "general",
            "claim_refs": list(self.claim_refs),
            "evidence_refs": list(self.evidence_refs),
            "assumption_refs": list(self.assumption_refs),
            "analysis_run_refs": list(self.analysis_run_refs),
            "artifact_version_refs": list(self.artifact_version_refs),
            "proof_review_refs": list(self.proof_review_refs),
            "planner_gap_refs": list(self.planner_gap_refs),
            "review_finding_refs": list(self.review_finding_refs),
            "contract_refs": list(self.contract_refs),
            "claims": [item.to_payload() for item in self.claims],
            "open_questions": list(self.open_questions),
            "counts": self.counts(),
            "warnings": list(self.warnings),
        }
        return payload


def project_research_brief(
    record: object = None,
    *,
    snapshot: object | None = None,
    findings: Iterable[object] = (),
    planner_gaps: Iterable[object] = (),
    proof_reviews: Iterable[object] = (),
    contracts: Iterable[object] = (),
    open_questions: Iterable[object] = (),
    profile_id: str = "general",
) -> ResearchBriefProjection | None:
    """Project a research record plus audit neighbors into one brief.

    Returns None when there is no valid record ref to anchor the projection;
    callers fail open instead of projecting half facts.
    """

    payload = _record_payload(record)
    if payload is None and snapshot is None:
        return None
    if snapshot is not None and not isinstance(snapshot, EvidenceRuntimeSnapshot):
        snapshot = None
    record_ref = snapshot.record_ref if snapshot else _generated_ref(
        (payload or {}).get("record_id"), "research_record"
    )
    if not record_ref:
        return None
    claims = _claim_summaries(payload or {})
    warnings: list[str] = []
    unsupported = sum(1 for item in claims if item.status == "unsupported")
    if unsupported:
        warnings.append("unsupported_claim_present")
    if snapshot is None:
        warnings.append("projection_without_runtime_snapshot")
    return ResearchBriefProjection(
        record_ref=record_ref,
        record_digest=(
            snapshot.record_digest
            if snapshot
            else content_digest((payload if isinstance(payload, Mapping) else {}).get("record_digest"))
        ),
        answer_status=_snapshot_answer(snapshot)
        or _status_token((payload or {}).get("answer_status"), ANSWER_STATUSES, default="not_answered"),
        profile_id=_identifier(profile_id, 80) or "general",
        claim_refs=_tuple_slice(snapshot.claim_refs if snapshot else _refs_from(claims)),
        evidence_refs=_tuple_slice(snapshot.evidence_refs if snapshot else ()),
        assumption_refs=_tuple_slice(snapshot.assumption_refs if snapshot else ()),
        analysis_run_refs=_tuple_slice(snapshot.analysis_run_refs if snapshot else ()),
        artifact_version_refs=_tuple_slice(snapshot.artifact_version_refs if snapshot else ()),
        source_count=len(snapshot.source_refs) if snapshot else _count_field(payload if isinstance(payload, Mapping) else {}, "sources"),
        proof_review_refs=_proof_refs(snapshot, proof_reviews),
        planner_gap_refs=_refs_from_items(planner_gaps, ("gap_id",), "planner_gap"),
        review_finding_refs=_refs_from_items(findings, ("finding_id",), "review_finding"),
        contract_refs=_code_tokens(contracts, ("contract_id",), 80),
        claims=tuple(claims[:MAX_BRIEF_CLAIMS]),
        open_questions=_text_items(open_questions, MAX_OPEN_QUESTIONS, MAX_ITEM_TEXT_CHARS),
        warnings=tuple(warnings)[:8],
    )


def _claim_summaries(payload: Mapping[str, object]) -> list[ClaimSummary]:
    rows_raw = payload.get("claims")
    if not isinstance(rows_raw, (list, tuple)):
        return []
    out: list[ClaimSummary] = []
    seen: set[str] = set()
    for row in rows_raw:
        if not isinstance(row, Mapping) or len(out) >= MAX_BRIEF_CLAIMS:
            continue
        ref = _normalize_runtime_ref(row.get("claim_id"), kind="claim")
        key = ref or _identifier(row.get("claim_text"), 40)
        if not key or key in seen:
            continue
        seen.add(key)
        evidence_count = len(row.get("evidence_refs") or ()) if isinstance(row.get("evidence_refs"), (list, tuple)) else 0
        out.append(ClaimSummary(
            claim_ref=ref,
            text=_clip(row.get("claim_text"), MAX_CLAIM_TEXT_CHARS),
            status=_status_token(row.get("status"), CLAIM_STATUSES, default="unsupported"),
            evidence_count=evidence_count,
        ))
    return out


def _record_payload(record: object) -> dict[str, object] | None:
    if isinstance(record, Mapping):
        return dict(record)
    to_jsonable = getattr(record, "to_jsonable", None)
    if callable(to_jsonable):
        try:
            data = to_jsonable()
        except Exception:
            return None
        return dict(data) if isinstance(data, Mapping) else None
    return None


def _proof_refs(
    snapshot: EvidenceRuntimeSnapshot | None,
    proof_reviews: Iterable[object],
) -> tuple[str, ...]:
    refs: list[str] = []
    if snapshot is not None and snapshot.proof_ref:
        refs.append(snapshot.proof_ref)
    for ref in _refs_from_items(proof_reviews, ("proof_ref",), "research_proof"):
        if ref not in refs:
            refs.append(ref)
        if len(refs) >= MAX_BRIEF_REFS:
            break
    return tuple(refs)


def _refs_from_items(items: Iterable[object], keys: tuple[str, ...], kind: str) -> tuple[str, ...]:
    refs: list[str] = []
    for item in items or ():
        to_payload = getattr(item, "to_payload", None)
        raw = to_payload() if callable(to_payload) else item
        if not isinstance(raw, Mapping):
            continue
        value = ""
        for key in keys:
            candidate = raw.get(key)
            if candidate:
                value = str(candidate)
                break
        ref = _normalize_runtime_ref(value, kind=kind)
        if ref and ref not in refs:
            refs.append(ref)
        if len(refs) >= MAX_BRIEF_REFS:
            break
    return tuple(refs)


def _code_tokens(items: Iterable[object], keys: tuple[str, ...], limit: int) -> tuple[str, ...]:
    out: list[str] = []
    for item in items or ():
        to_payload = getattr(item, "to_payload", None)
        raw = to_payload() if callable(to_payload) else item
        if not isinstance(raw, Mapping):
            continue
        for key in keys:
            token = _identifier(raw.get(key), limit)
            if token and token not in out:
                out.append(token)
                break
        if len(out) >= 8:
            break
    return tuple(out)


def _refs_from(claims: Iterable[ClaimSummary]) -> tuple[str, ...]:
    refs: list[str] = []
    for item in claims:
        if item.claim_ref and item.claim_ref not in refs:
            refs.append(item.claim_ref)
    return tuple(refs)


def _tuple_slice(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(values[:MAX_BRIEF_REFS])


def _text_items(values: Iterable[object], limit: int, width: int) -> tuple[str, ...]:
    out: list[str] = []
    for value in values or ():
        text = _clip(str(value or "").replace("\n", " "), width)
        if text and text.casefold() not in {row.casefold() for row in out}:
            out.append(text)
        if len(out) >= max(0, limit):
            break
    return tuple(out)


def _snapshot_answer(snapshot: EvidenceRuntimeSnapshot | None) -> str:
    if snapshot is None:
        return ""
    return snapshot.answer_status if snapshot.answer_status in ANSWER_STATUSES else ""


def _count_field(payload: Mapping[str, object], key: str) -> int:
    rows = payload.get(key)
    return len(rows) if isinstance(rows, (list, tuple)) else 0


__all__ = [
    "ANSWER_STATUSES",
    "CLAIM_STATUSES",
    "ClaimSummary",
    "ResearchBriefProjection",
    "project_research_brief",
]
