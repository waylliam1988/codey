"""Refs-only Research-to-Code handoff projection.

Turns facts that already exist (research records, evidence runtime refs,
review findings, proof reviews, contracts) into one bounded projection plus
an explicit implementation-impact contract, so downstream consumers never
re-parse long markdown reports to guess structure.

Hard rules:

- The projection carries validated runtime refs and bounded claim/assumption
  texts only. Raw synthesis bodies, webpage content, and transcripts never
  enter it.
- An unsupported claim can surface as uncertainty/risk; it can never back an
  implementation constraint.
- ``test_suggestions`` are context for the writer, never tool authorization.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Mapping

from codey.refs import clip as _clip
from codey.refs import digest_ref as _digest_ref
from codey.refs import identifier as _identifier
from codey.research.evidence_runtime import (
    EvidenceRuntimeSnapshot,
    normalize_runtime_ref as _normalize_runtime_ref,
)
from codey.research.shape import generated_ref as _generated_ref
from codey.text_budget import clip_middle


MAX_HANDOFF_CHARS = 6000
MAX_BRIEF_CLAIMS = 16
MAX_BRIEF_REFS = 24
MAX_BRIEF_TEXT_ITEMS = 6
MAX_OPEN_QUESTIONS = 5
MAX_CLAIM_TEXT_CHARS = 260
MAX_ITEM_TEXT_CHARS = 220
MAX_IMPACT_FILES = 12
MAX_IMPACT_ITEMS = 8
ANSWER_STATUSES = frozenset({
    "answered",
    "partial",
    "insufficient_evidence",
    "not_answered",
})
CLAIM_STATUSES = frozenset({"evidence_backed", "assumption", "unsupported"})
CONSTRAINT_SUPPORTS = frozenset({"verified", "assumption_risk"})
_PATH_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+ -]*(/[A-Za-z0-9._@+ -]*)*$")


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
            "status": _claim_status(self.status),
            "evidence_count": max(0, int(self.evidence_count or 0)),
        }


@dataclass(frozen=True)
class ImplementationConstraint:
    """One implementation constraint backed by explicit research support."""

    text: str
    support: str = "verified"
    claim_refs: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, object]:
        return {
            "text": _clip(self.text, MAX_ITEM_TEXT_CHARS),
            "support": _support_token(self.support),
            "claim_refs": list(self.claim_refs[:4]),
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
            "sources": int(self.source_count),
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
            "answer_status": _answer_status(self.answer_status),
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


@dataclass(frozen=True)
class ResearchImpactContract:
    affected_files: tuple[str, ...] = ()
    implementation_constraints: tuple[ImplementationConstraint, ...] = ()
    test_suggestions: tuple[str, ...] = ()
    risk_notes: tuple[str, ...] = ()
    out_of_scope_items: tuple[str, ...] = ()
    decision_refs: tuple[str, ...] = ()

    def is_empty(self) -> bool:
        return not (
            self.affected_files
            or self.implementation_constraints
            or self.test_suggestions
            or self.risk_notes
            or self.out_of_scope_items
            or self.decision_refs
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "affected_files": list(self.affected_files),
            "implementation_constraints": [
                item.to_payload() for item in self.implementation_constraints
            ],
            "test_suggestions": list(self.test_suggestions),
            "risk_notes": list(self.risk_notes),
            "out_of_scope_items": list(self.out_of_scope_items),
            "decision_refs": list(self.decision_refs),
        }


def project_research_brief(
    record: object = None,
    *,
    snapshot: EvidenceRuntimeSnapshot | None = None,
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
        record_digest=(snapshot.record_digest if snapshot else _digest_ref((payload or {}).get("record_digest"))),
        answer_status=_snapshot_answer(snapshot) or _answer_status((payload or {}).get("answer_status")),
        profile_id=_identifier(profile_id, 80) or "general",
        claim_refs=_tuple_slice(snapshot.claim_refs if snapshot else _refs_from(claims)),
        evidence_refs=_tuple_slice(snapshot.evidence_refs if snapshot else ()),
        assumption_refs=_tuple_slice(snapshot.assumption_refs if snapshot else ()),
        analysis_run_refs=_tuple_slice(snapshot.analysis_run_refs if snapshot else ()),
        artifact_version_refs=_tuple_slice(snapshot.artifact_version_refs if snapshot else ()),
        source_count=len(snapshot.source_refs) if snapshot else _count_field(payload, "sources"),
        proof_review_refs=_proof_refs(snapshot, proof_reviews),
        planner_gap_refs=_refs_from_items(planner_gaps, ("gap_id",), "planner_gap"),
        review_finding_refs=_refs_from_items(findings, ("finding_id",), "review_finding"),
        contract_refs=_code_tokens(contracts, ("contract_id",), 80),
        claims=tuple(claims[:MAX_BRIEF_CLAIMS]),
        open_questions=_text_items(open_questions, MAX_OPEN_QUESTIONS, MAX_ITEM_TEXT_CHARS),
        warnings=tuple(warnings)[:8],
    )


def constraints_from_claims(claims: Iterable[ClaimSummary]) -> tuple[ImplementationConstraint, ...]:
    """Verified-support constraints derivable from brief claims."""

    out: list[ImplementationConstraint] = []
    for item in claims or ():
        if not isinstance(item, ClaimSummary):
            continue
        if item.status != "evidence_backed" or not item.claim_ref:
            continue
        out.append(ImplementationConstraint(
            text=item.text,
            support="verified",
            claim_refs=(item.claim_ref,),
        ))
        if len(out) >= MAX_IMPACT_ITEMS:
            break
    return tuple(out)


def build_impact_contract(
    *,
    claims: Iterable[object] = (),
    affected_files: Iterable[object] = (),
    test_suggestions: Iterable[object] = (),
    risk_notes: Iterable[object] = (),
    out_of_scope_items: Iterable[object] = (),
    decision_refs: Iterable[object] = (),
) -> ResearchImpactContract | None:
    """Build the impact contract, enforcing the support boundary.

    ``claims`` feed verified constraints automatically; assumption-backed or
    unsupported claims are demoted into ``risk_notes``. Explicitly passed
    files/tests/risks stay bounded context only -- nothing here authorizes a
    tool or changes permissions.
    """

    summaries = [
        item for item in claims or () if isinstance(item, ClaimSummary)
    ]
    constraints = list(constraints_from_claims(summaries))
    risks = _text_items(risk_notes, MAX_IMPACT_ITEMS, MAX_ITEM_TEXT_CHARS)
    for item in summaries:
        if item.status == "unsupported":
            entry = f"{item.text} [unsupported_claim]"
            if entry not in risks and len(risks) < MAX_IMPACT_ITEMS:
                risks = (*risks, entry)
        elif item.status == "assumption":
            entry = f"{item.text} [declared_assumption]"
            if entry not in risks and len(risks) < MAX_IMPACT_ITEMS:
                risks = (*risks, entry)
    contract = ResearchImpactContract(
        affected_files=_file_tokens(affected_files),
        implementation_constraints=tuple(constraints),
        test_suggestions=_text_items(test_suggestions, MAX_IMPACT_ITEMS, MAX_ITEM_TEXT_CHARS),
        risk_notes=risks,
        out_of_scope_items=_text_items(out_of_scope_items, MAX_IMPACT_ITEMS, MAX_ITEM_TEXT_CHARS),
        decision_refs=_bounded_ids(decision_refs, 8),
    )
    if contract.is_empty():
        return None
    return contract


def render_handoff(
    projection: ResearchBriefProjection,
    impact: ResearchImpactContract | None = None,
) -> str:
    """Render the short model-visible handoff for one projection."""

    lines: list[str] = []
    status = _answer_status(projection.answer_status)
    counts = projection.counts()
    summary = (
        f"- record: {projection.record_ref}"
        + (f" ({status})" if status != "not_answered" else "")
        + f"; evidence refs: {counts['evidence']} across {counts['sources']} sources"
    )
    lines.append(summary)
    supported = projection.supported_claims()
    if supported:
        lines.append("Concluded (verified support):")
        lines.extend(f"- {_render_claim(item)}" for item in supported)
    uncertain = projection.uncertain_claims()
    if uncertain:
        lines.append("Uncertain / assumptions:")
        lines.extend(f"- {_render_claim(item)}" for item in uncertain)
    if projection.open_questions:
        lines.append("Open questions:")
        lines.extend(f"- {item}" for item in projection.open_questions)
    if projection.warnings:
        lines.append("Evidence caveats: " + ", ".join(projection.warnings))
    if impact is not None and not impact.is_empty():
        lines.extend(_impact_lines(impact))
    rendered, _truncated = clip_middle("\n".join(lines), MAX_HANDOFF_CHARS)
    return rendered.strip()


def _impact_lines(impact: ResearchImpactContract) -> list[str]:
    lines = ["Implementation impact (research-derived, context only):"]
    if impact.affected_files:
        lines.append("- files likely affected: " + ", ".join(impact.affected_files))
    for item in impact.implementation_constraints:
        tag = "verified" if item.support == "verified" else "assumption+risk"
        lines.append(f"- constraint [{tag}]: {item.text}")
    for item in impact.risk_notes:
        lines.append(f"- risk: {item}")
    if impact.test_suggestions:
        lines.append("- tests to consider (not authorized by this handoff):")
        lines.extend(f"  - {item}" for item in impact.test_suggestions)
    if impact.out_of_scope_items:
        lines.append("- out of scope: " + "; ".join(impact.out_of_scope_items))
    return lines


def _render_claim(item: ClaimSummary) -> str:
    suffix = f" [{item.claim_ref}]" if item.claim_ref else ""
    if item.status == "unsupported":
        suffix += " [unsupported]"
    elif item.status == "assumption":
        suffix += " [assumption]"
    return f"{_clip(item.text, MAX_CLAIM_TEXT_CHARS)}{suffix}"


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
            status=_claim_status(row.get("status")),
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
        raw = item.to_payload() if callable(getattr(item, "to_payload", None)) else item
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
        raw = item.to_payload() if callable(getattr(item, "to_payload", None)) else item
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
        if len(out) >= max(0, int(limit)):
            break
    return tuple(out)


def _bounded_ids(values: Iterable[object], limit: int) -> tuple[str, ...]:
    out: list[str] = []
    for value in values or ():
        token = _identifier(value, 120)
        if token and token not in out:
            out.append(token)
        if len(out) >= max(0, int(limit)):
            break
    return tuple(out)


def _file_tokens(values: Iterable[object]) -> tuple[str, ...]:
    out: list[str] = []
    for value in values or ():
        text = str(value or "").strip().replace("\\", "/")
        if not text or len(text) > 200:
            continue
        if text.startswith(("/", "~")) or ".." in text or ":" in text:
            continue
        if not _PATH_TOKEN_RE.match(text):
            continue
        normalized = text.strip("./")
        if normalized and normalized not in out:
            out.append(normalized)
        if len(out) >= MAX_IMPACT_FILES:
            break
    return tuple(out)


def _snapshot_answer(snapshot: EvidenceRuntimeSnapshot | None) -> str:
    if snapshot is None:
        return ""
    return snapshot.answer_status if snapshot.answer_status in ANSWER_STATUSES else ""


def _answer_status(value: object) -> str:
    text = _identifier(value, 40).lower()
    return text if text in ANSWER_STATUSES else "not_answered"


def _claim_status(value: object) -> str:
    text = _identifier(value, 40).lower()
    return text if text in CLAIM_STATUSES else "unsupported"


def _support_token(value: object) -> str:
    text = _identifier(value, 20).lower()
    return text if text in CONSTRAINT_SUPPORTS else "verified"


def _count_field(payload: Mapping[str, object], key: str) -> int:
    rows = payload.get(key)
    return len(rows) if isinstance(rows, (list, tuple)) else 0


__all__ = [
    "ANSWER_STATUSES",
    "CLAIM_STATUSES",
    "CONSTRAINT_SUPPORTS",
    "ClaimSummary",
    "ImplementationConstraint",
    "MAX_HANDOFF_CHARS",
    "ResearchBriefProjection",
    "ResearchImpactContract",
    "build_impact_contract",
    "constraints_from_claims",
    "project_research_brief",
    "render_handoff",
]
