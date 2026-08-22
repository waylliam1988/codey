"""Deterministic ReviewFinding core over evidence facts.

ReviewFindingRecord is an audit read model: it says which claim, evidence,
source, analysis run, or record has a proof problem, how severe it is, and
whether a later verification event resolved it. Findings are projected from
facts that already exist (proof-review diagnostics, failed analysis runs);
they never call models, execute searches, or mutate research state.

``confirmed`` status can only be reached through :class:`ReviewFindingEvent`
with a ``verified_by`` value from the deterministic allowlist. A model
claiming "fixed" is not a verification.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable, Mapping

from codey.research.analysis_run import REPRODUCTION_FAILED
from codey.research.evidence_runtime import (
    EvidenceRuntimeSnapshot,
    normalize_runtime_ref,
)
from codey.research.identity import bounded_refs, identifier, stable_ref
from codey.research.proof_quality import MAX_DIAGNOSTICS, ProofDiagnostic, ResearchProofReview
from codey.research.shape import generated_ref as _generated_ref


FINDING_UNSUPPORTED_CLAIM = "unsupported_claim"
FINDING_CITATION_MISMATCH = "citation_mismatch"
FINDING_STALE_SOURCE = "stale_source"
FINDING_OVERREACH = "overreach"
FINDING_MISSING_COUNTEREVIDENCE = "missing_counterevidence"
FINDING_CONTRADICTORY_SOURCES = "contradictory_sources"
FINDING_SOURCE_CONFLICT = "source_conflict"
FINDING_FAILED_ANALYSIS_SUPPORT = "failed_analysis_support"
FINDING_QUALIFIED_SUPPORT = "qualified_support"

FINDING_KINDS = frozenset({
    FINDING_UNSUPPORTED_CLAIM,
    FINDING_CITATION_MISMATCH,
    FINDING_STALE_SOURCE,
    FINDING_OVERREACH,
    FINDING_MISSING_COUNTEREVIDENCE,
    FINDING_CONTRADICTORY_SOURCES,
    FINDING_SOURCE_CONFLICT,
    FINDING_FAILED_ANALYSIS_SUPPORT,
    FINDING_QUALIFIED_SUPPORT,
})

SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"
FINDING_SEVERITIES = frozenset({SEVERITY_INFO, SEVERITY_WARNING, SEVERITY_CRITICAL})
_SEVERITY_RANK = {SEVERITY_INFO: 0, SEVERITY_WARNING: 1, SEVERITY_CRITICAL: 2}

STATUS_OPEN = "open"
STATUS_ADDRESSED = "addressed"
STATUS_CONFIRMED = "confirmed"
STATUS_REJECTED = "rejected"
FINDING_STATUSES = frozenset({STATUS_OPEN, STATUS_ADDRESSED, STATUS_CONFIRMED, STATUS_REJECTED})

EVENT_ADDRESSED = "addressed"
EVENT_CONFIRMED = "confirmed"
EVENT_REJECTED = "rejected"
EVENT_ACTIONS = frozenset({EVENT_ADDRESSED, EVENT_CONFIRMED, EVENT_REJECTED})

# A finding may only be confirmed by a verification fact, never by a model
# self-report that the problem went away.
CONFIRMATION_SOURCES = frozenset({
    "deterministic_check",
    "analysis_run",
    "opened_source_evidence",
    "reviewer_pass",
})

GAP_FOLLOWUP_SEARCH = "followup_search"
GAP_LOCATOR_VERIFICATION = "locator_verification"
GAP_COUNTEREVIDENCE_SEARCH = "counterevidence_search"
GAP_REFRESH_QUERY = "refresh_query"
GAP_RERUN_ANALYSIS = "rerun_analysis"
GAP_KINDS = frozenset({
    GAP_FOLLOWUP_SEARCH,
    GAP_LOCATOR_VERIFICATION,
    GAP_COUNTEREVIDENCE_SEARCH,
    GAP_REFRESH_QUERY,
    GAP_RERUN_ANALYSIS,
})

# One table owns reason-code interpretation for both finding kind and severity.
_DIAGNOSTIC_FINDINGS: dict[str, tuple[str, str]] = {
    "claim_not_evidence_backed": (FINDING_UNSUPPORTED_CLAIM, SEVERITY_CRITICAL),
    "claim_missing_support_relation": (FINDING_UNSUPPORTED_CLAIM, SEVERITY_CRITICAL),
    "support_relation_not_claim_evidence": (FINDING_UNSUPPORTED_CLAIM, SEVERITY_CRITICAL),
    "assumption_used_as_answer": (FINDING_UNSUPPORTED_CLAIM, SEVERITY_CRITICAL),
    "claim_missing_citation": (FINDING_CITATION_MISMATCH, SEVERITY_WARNING),
    "claim_missing_evidence_ref": (FINDING_CITATION_MISMATCH, SEVERITY_WARNING),
    "claim_missing_assumption_ref": (FINDING_CITATION_MISMATCH, SEVERITY_WARNING),
    "support_relation_missing_evidence": (FINDING_CITATION_MISMATCH, SEVERITY_WARNING),
    "support_relation_wrong_stance": (FINDING_CITATION_MISMATCH, SEVERITY_WARNING),
    "relation_missing_claim": (FINDING_CITATION_MISMATCH, SEVERITY_WARNING),
    "counter_relation_missing_target": (FINDING_CITATION_MISMATCH, SEVERITY_WARNING),
    "support_relation_bad_locator": (FINDING_CITATION_MISMATCH, SEVERITY_WARNING),
    "counter_relation_bad_locator": (FINDING_CITATION_MISMATCH, SEVERITY_WARNING),
}

_GAP_KIND_BY_FINDING: dict[str, str] = {
    FINDING_UNSUPPORTED_CLAIM: GAP_FOLLOWUP_SEARCH,
    FINDING_OVERREACH: GAP_FOLLOWUP_SEARCH,
    FINDING_CITATION_MISMATCH: GAP_LOCATOR_VERIFICATION,
    FINDING_MISSING_COUNTEREVIDENCE: GAP_COUNTEREVIDENCE_SEARCH,
    FINDING_STALE_SOURCE: GAP_REFRESH_QUERY,
    FINDING_FAILED_ANALYSIS_SUPPORT: GAP_RERUN_ANALYSIS,
}

MAX_FINDING_REASONS = 8
MAX_GAP_FINDING_REFS = 4
MAX_PLANNER_GAPS = 16


@dataclass(frozen=True)
class ReviewFindingRecord:
    finding_id: str
    kind: str
    severity: str
    status: str = STATUS_OPEN
    target_ref: str = ""
    claim_ref: str = ""
    evidence_ref: str = ""
    source_ref: str = ""
    analysis_run_ref: str = ""
    artifact_ref: str = ""
    proof_ref: str = ""
    reason_codes: tuple[str, ...] = ()
    addressed_by: tuple[str, ...] = ()
    confirmed_by: tuple[str, ...] = ()
    message: str = ""

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "finding_id": self.finding_id,
            "kind": identifier(self.kind, 40),
            "severity": identifier(self.severity, 20),
            "status": identifier(self.status, 20),
            "target_ref": self.target_ref,
            "claim_ref": self.claim_ref,
            "evidence_ref": self.evidence_ref,
            "source_ref": self.source_ref,
            "analysis_run_ref": self.analysis_run_ref,
            "artifact_ref": self.artifact_ref,
            "proof_ref": self.proof_ref,
            "reason_codes": list(bounded_refs(self.reason_codes, limit=MAX_FINDING_REASONS)),
            "addressed_by": list(bounded_refs(self.addressed_by, limit=MAX_FINDING_REASONS)),
            "confirmed_by": list(bounded_refs(self.confirmed_by, limit=MAX_FINDING_REASONS)),
        }
        return payload


@dataclass(frozen=True)
class PlannerGap:
    """A follow-up need derived from findings; it plans nothing by itself."""

    gap_id: str
    gap_kind: str
    target_ref: str = ""
    reason_codes: tuple[str, ...] = ()
    finding_refs: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, object]:
        return {
            "gap_id": self.gap_id,
            "gap_kind": identifier(self.gap_kind, 40),
            "target_ref": self.target_ref,
            "reason_codes": list(bounded_refs(self.reason_codes, limit=MAX_FINDING_REASONS)),
            "finding_refs": list(bounded_refs(self.finding_refs, limit=MAX_GAP_FINDING_REFS)),
        }


@dataclass(frozen=True)
class ReviewFindingEvent:
    action: str
    finding_ref: str = ""
    verified_by: str = ""
    reason_code: str = ""


def findings_from_proof_review(
    review: ResearchProofReview | Mapping[str, object] | None,
    snapshot: EvidenceRuntimeSnapshot | None = None,
) -> tuple[ReviewFindingRecord, ...]:
    """Project proof-review diagnostics and warnings into located findings.

    When a snapshot is supplied, refs that do not resolve inside its graph are
    dropped, so a finding can never point at evidence the record does not own.
    """

    if review is None:
        return ()
    diagnostics = _diagnostics_of(review)
    proof_ref = _generated_ref(_field(review, "proof_ref"), "research_proof")
    record_ref = _generated_ref(_field(review, "record_id"), "research_record")
    groups: dict[tuple[str, ...], dict[str, object]] = {}

    def add(
        kind: str,
        severity: str,
        *,
        claim_ref: str = "",
        evidence_ref: str = "",
        source_ref: str = "",
        analysis_run_ref: str = "",
        reason_code: str = "",
    ) -> None:
        target = claim_ref or proof_ref or record_ref
        if not target:
            return
        key = (kind, target, claim_ref, evidence_ref, source_ref, analysis_run_ref)
        group = groups.get(key)
        if group is None:
            group = {"severity": severity, "reasons": []}
            groups[key] = group
        reasons: list[str] = group["reasons"]  # type: ignore[assignment]
        if reason_code and reason_code not in reasons:
            reasons.append(reason_code)
        if _SEVERITY_RANK[severity] > _SEVERITY_RANK[group["severity"]]:  # type: ignore[index]
            group["severity"] = severity

    for diag in diagnostics:
        mapped = _DIAGNOSTIC_FINDINGS.get(diag.reason_code)
        if mapped is None:
            continue
        kind, severity = mapped
        add(
            kind,
            severity,
            claim_ref=_resolve(diag.claim_ref, "claim", snapshot),
            evidence_ref=_resolve(diag.evidence_ref, "evidence", snapshot),
            source_ref=_resolve(diag.source_ref, "source", snapshot),
            reason_code=diag.reason_code,
        )

    missing = tuple(_text_items(_field(review, "missing_evidence")))
    if "counterevidence_not_checked" in missing:
        add(
            FINDING_MISSING_COUNTEREVIDENCE,
            SEVERITY_WARNING,
            reason_code="counterevidence_not_checked",
        )
    for warning in _text_items(_field(review, "stale_warnings")):
        add(FINDING_STALE_SOURCE, SEVERITY_WARNING, reason_code=identifier(warning, 80))
    for warning in _text_items(_field(review, "overclaim_warnings")):
        add(FINDING_OVERREACH, SEVERITY_WARNING, reason_code=identifier(warning, 80))

    findings: list[ReviewFindingRecord] = []
    for key in sorted(groups):
        kind, target, claim_ref, evidence_ref, source_ref, analysis_run_ref = key
        group = groups[key]
        reasons = tuple(bounded_refs(group["reasons"], limit=MAX_FINDING_REASONS))  # type: ignore[arg-type]
        if not reasons:
            continue
        findings.append(ReviewFindingRecord(
            finding_id=stable_ref(
                "review_finding",
                kind,
                target,
                claim_ref,
                evidence_ref,
                source_ref,
                analysis_run_ref,
                reasons,
            ),
            kind=kind,
            severity=str(group["severity"]),  # type: ignore[arg-type]
            status=STATUS_OPEN,
            target_ref=target,
            claim_ref=claim_ref,
            evidence_ref=evidence_ref,
            source_ref=source_ref,
            analysis_run_ref=analysis_run_ref,
            proof_ref=proof_ref,
            reason_codes=reasons,
        ))
    return tuple(findings)


def failed_analysis_findings(
    analysis_runs: Iterable[object],
) -> tuple[ReviewFindingRecord, ...]:
    """Project failed AnalysisRun records into support findings.

    v1 wiring note: ResearchPipeline does not execute local commands, so no
    producer feeds this projection yet; it exists as the deterministic core
    for the version where reports start citing ``analysis_run:<id>``.
    """

    findings: list[ReviewFindingRecord] = []
    for run in analysis_runs or ():
        run_ref = normalize_runtime_ref(_field(run, "analysis_run_id"), kind="analysis_run")
        if not run_ref:
            continue
        status = identifier(_field(run, "reproduction_status"), 40)
        if bool(_field(run, "ok")) or status != REPRODUCTION_FAILED:
            continue
        findings.append(ReviewFindingRecord(
            finding_id=stable_ref("review_finding", FINDING_FAILED_ANALYSIS_SUPPORT, run_ref),
            kind=FINDING_FAILED_ANALYSIS_SUPPORT,
            severity=SEVERITY_CRITICAL,
            target_ref=run_ref,
            analysis_run_ref=run_ref,
            reason_codes=("failed_analysis",),
        ))
    return tuple(findings)


def planner_gaps_from_findings(
    findings: Iterable[object],
    *,
    limit: int = MAX_PLANNER_GAPS,
) -> tuple[PlannerGap, ...]:
    """Project findings into bounded planner gap read models.

    Gaps describe what kind of follow-up would address a finding. They do not
    schedule anything and they carry no raw text.
    """

    gaps: list[PlannerGap] = []
    seen: set[tuple[str, str]] = set()
    for finding in findings or ():
        if len(gaps) >= max(0, int(limit)):
            break
        kind = identifier(_field(finding, "kind"), 40)
        mapped = _GAP_KIND_BY_FINDING.get(kind)
        if mapped is None:
            continue
        target = (
            normalize_runtime_ref(_field(finding, "target_ref"))
            or _generated_ref(_field(finding, "proof_ref"), "research_proof")
        )
        if not target:
            continue
        key = (mapped, target)
        if key in seen:
            continue
        seen.add(key)
        finding_id = normalize_runtime_ref(_field(finding, "finding_id"), kind="review_finding")
        gaps.append(PlannerGap(
            gap_id=stable_ref("planner_gap", mapped, target),
            gap_kind=mapped,
            target_ref=target,
            reason_codes=tuple(bounded_refs(_field(finding, "reason_codes"), limit=MAX_FINDING_REASONS)),
            finding_refs=(finding_id,) if finding_id else (),
        ))
    return tuple(gaps)


def apply_finding_events(
    findings: Iterable[object],
    events: Iterable[object],
) -> tuple[ReviewFindingRecord, ...]:
    """Apply lifecycle events append-only; unknown or invalid events no-op.

    Lifecycle rules:

    - ``addressed`` moves open findings forward.
    - ``confirmed`` requires ``verified_by`` from CONFIRMATION_SOURCES; model
      self-reports fail closed and leave the finding untouched.
    - ``rejected`` closes open/addressed findings.
    """

    rows = [item for item in (findings or ()) if isinstance(item, ReviewFindingRecord)]
    if not rows:
        return ()
    updated = {row.finding_id: row for row in rows}
    for event in events or ():
        action = identifier(_field(event, "action"), 20)
        if action not in EVENT_ACTIONS:
            continue
        ref = normalize_runtime_ref(_field(event, "finding_ref"), kind="review_finding")
        current = updated.get(ref)
        if current is None:
            continue
        if action == EVENT_ADDRESSED:
            if current.status == STATUS_OPEN:
                updated[ref] = replace(
                    current,
                    status=STATUS_ADDRESSED,
                    addressed_by=_append_reason(current.addressed_by, event),
                )
        elif action == EVENT_CONFIRMED:
            verified_by = identifier(_field(event, "verified_by"), 40)
            if (
                current.status in {STATUS_OPEN, STATUS_ADDRESSED}
                and verified_by in CONFIRMATION_SOURCES
            ):
                updated[ref] = replace(
                    current,
                    status=STATUS_CONFIRMED,
                    confirmed_by=_append_reason(current.confirmed_by, event),
                )
        elif action == EVENT_REJECTED:
            if current.status in {STATUS_OPEN, STATUS_ADDRESSED}:
                updated[ref] = replace(current, status=STATUS_REJECTED)
    return tuple(updated[row.finding_id] for row in rows)


def review_finding_trace_payloads(findings: Iterable[object]) -> list[dict[str, object]]:
    """Bounded trace projection of findings: refs and codes only."""

    payloads: list[dict[str, object]] = []
    for finding in _items(findings):
        finding_id = _generated_ref(_field(finding, "finding_id"), "review_finding")
        kind = identifier(_field(finding, "kind"), 40)
        if not finding_id or kind not in FINDING_KINDS:
            continue
        severity = identifier(_field(finding, "severity"), 20)
        status = identifier(_field(finding, "status"), 20)
        payload: dict[str, object] = {
            "finding_id": finding_id,
            "kind": kind,
            "severity": severity if severity in FINDING_SEVERITIES else SEVERITY_WARNING,
            "status": status if status in FINDING_STATUSES else STATUS_OPEN,
            "target_ref": normalize_runtime_ref(_field(finding, "target_ref")),
            "reason_codes": [
                code
                for code in bounded_refs(_field(finding, "reason_codes"), limit=MAX_FINDING_REASONS)
            ],
        }
        for key, ref_kind in (
            ("claim_ref", "claim"),
            ("evidence_ref", "evidence"),
            ("source_ref", "source"),
            ("analysis_run_ref", "analysis_run"),
            ("artifact_ref", "artifact_version"),
            ("proof_ref", "research_proof"),
        ):
            ref = normalize_runtime_ref(_field(finding, key), kind=ref_kind)
            if ref:
                payload[key] = ref
        payloads.append(payload)
    return payloads


def planner_gap_trace_payloads(gaps: Iterable[object]) -> list[dict[str, object]]:
    """Bounded trace projection of planner gaps: refs and codes only."""

    payloads: list[dict[str, object]] = []
    for gap in _items(gaps):
        gap_id = _generated_ref(_field(gap, "gap_id"), "planner_gap")
        gap_kind = identifier(_field(gap, "gap_kind"), 40)
        if not gap_id or gap_kind not in GAP_KINDS:
            continue
        payloads.append({
            "gap_id": gap_id,
            "gap_kind": gap_kind,
            "target_ref": normalize_runtime_ref(_field(gap, "target_ref")),
            "reason_codes": list(bounded_refs(_field(gap, "reason_codes"), limit=MAX_FINDING_REASONS)),
            "finding_refs": [
                ref
                for ref in (
                    normalize_runtime_ref(item, kind="review_finding")
                    for item in _text_items(_field(gap, "finding_refs"))
                )
                if ref
            ][:MAX_GAP_FINDING_REFS],
        })
    return payloads


def _diagnostics_of(review: ResearchProofReview | Mapping[str, object]) -> list[ProofDiagnostic]:
    if isinstance(review, ResearchProofReview):
        return list(review.diagnostics[:MAX_DIAGNOSTICS])
    rows = review.get("diagnostics") if isinstance(review, Mapping) else None
    out: list[ProofDiagnostic] = []
    if not isinstance(rows, (list, tuple)):
        return out
    for row in rows[:MAX_DIAGNOSTICS]:
        if isinstance(row, ProofDiagnostic):
            out.append(row)
            continue
        if isinstance(row, Mapping):
            out.append(ProofDiagnostic(
                reason_code=identifier(row.get("reason_code"), 80),
                claim_ref=normalize_runtime_ref(row.get("claim_ref"), kind="claim"),
                evidence_ref=normalize_runtime_ref(row.get("evidence_ref"), kind="evidence"),
                source_ref=normalize_runtime_ref(row.get("source_ref"), kind="source"),
                relation_ref=normalize_runtime_ref(row.get("relation_ref"), kind="relation"),
            ))
    return out


def _resolve(ref: str, kind: str, snapshot: EvidenceRuntimeSnapshot | None) -> str:
    normalized = normalize_runtime_ref(ref, kind=kind)
    if not normalized or snapshot is None:
        return normalized
    bucket = {
        "claim": snapshot.claim_refs,
        "evidence": snapshot.evidence_refs,
        "source": snapshot.source_refs,
    }.get(kind)
    if bucket is not None and normalized not in bucket:
        return ""
    return normalized


def _append_reason(reasons: tuple[str, ...], event: object) -> tuple[str, ...]:
    code = identifier(_field(event, "reason_code"), 80) or identifier(_field(event, "verified_by"), 80)
    if not code or code in reasons:
        return reasons
    return (*bounded_refs(reasons, limit=MAX_FINDING_REASONS), code)


def _field(item: object, name: str) -> object:
    if isinstance(item, Mapping):
        return item.get(name)
    return getattr(item, name, "")


def _items(values: Iterable[object]) -> tuple[object, ...]:
    if values is None:
        return ()
    if isinstance(values, (list, tuple)):
        return tuple(values)
    if hasattr(values, "__iter__"):
        return tuple(values)
    return (values,)


def _text_items(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(str(item or "") for item in value)
    return ()


__all__ = [
    "CONFIRMATION_SOURCES",
    "EVENT_ACTIONS",
    "FINDING_CITATION_MISMATCH",
    "FINDING_CONTRADICTORY_SOURCES",
    "FINDING_FAILED_ANALYSIS_SUPPORT",
    "FINDING_MISSING_COUNTEREVIDENCE",
    "FINDING_OVERREACH",
    "FINDING_QUALIFIED_SUPPORT",
    "FINDING_SOURCE_CONFLICT",
    "FINDING_STATUSES",
    "FINDING_STALE_SOURCE",
    "FINDING_UNSUPPORTED_CLAIM",
    "FINDING_SEVERITIES",
    "FINDING_KINDS",
    "GAP_KINDS",
    "MAX_PLANNER_GAPS",
    "PlannerGap",
    "ReviewFindingEvent",
    "ReviewFindingRecord",
    "SEVERITY_CRITICAL",
    "SEVERITY_INFO",
    "SEVERITY_WARNING",
    "STATUS_CONFIRMED",
    "apply_finding_events",
    "failed_analysis_findings",
    "findings_from_proof_review",
    "planner_gap_trace_payloads",
    "planner_gaps_from_findings",
    "review_finding_trace_payloads",
]
