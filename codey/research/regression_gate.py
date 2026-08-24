"""Research regression gate: one bounded read model over the evidence stack.

This module is the 0.4.11 evaluation spine's pure core. It consumes facts that
already exist elsewhere -- Evidence Runtime snapshots, proof reviews, brief
projections, impact contracts, review findings, planner gaps, reproducibility
capsules, completion proofs, and pipeline summaries -- and projects them into
one deterministic report of bounded metrics, boolean observables, and a gate
verdict.

Hard rules:

- Projection-only: no I/O, no models, no providers, no fetching, no journal.
- Output carries only refs, digests, counts, allow-listed statuses, booleans,
  and small numbers. Raw prompts, replies, transcripts, and source bodies can
  never enter a report by construction.
- Observables state what was observed; expectations compare against those
  observations. Unknown expectation keys fail closed instead of being ignored.
- A false completion is recorded as a metric, never enforced here. Blocking a
  model-visible ``done`` stays with the 0.4.13 enforcement layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from codey.refs import digest_json, identifier, nonnegative_int, stable_ref
from codey.research.brief_projection import (
    ANSWER_STATUSES,
    CLAIM_STATUSES,
    CONSTRAINT_SUPPORTS,
)
from codey.research.review_finding import (
    FINDING_CONTRADICTORY_SOURCES,
    FINDING_SOURCE_CONFLICT,
    FINDING_STALE_SOURCE,
    SEVERITY_CRITICAL,
    STATUS_OPEN,
)
from codey.research.source_trust import project_source_set


MAX_FINDINGS_SCANNED = 64
MAX_RELATIONS_SCANNED = 64
MAX_SOURCES_PROJECTED = 24
MAX_EXPECTATION_KEYS = 24
MAX_REASON_CODES = 12

CAPSULE_STATUSES = frozenset({
    "output_captured",
    "output_not_captured",
    "failed",
    "no_analysis_runs",
})

OBSERVABLE_NAMES = frozenset({
    "answered",
    "partial_answer",
    "stale_source_flagged",
    "unsupported_claim_present",
    "unsupported_in_constraints",
    "counterevidence_checked",
    "citation_locator_verified",
    "support_relation_verified",
    "ledger_record_verified",
    "overclaim_warned",
    "conflicting_evidence_finding",
    "planner_gap_created",
    "analysis_run_observed",
    "reproducible_analysis",
    "analysis_run_failed",
    "new_evidence_after_followup",
    "false_completion_candidate",
})

METRIC_NAMES = frozenset({
    "answer_status",
    "answer_coverage_score",
    "grounded_ratio",
    "grounded_claim_count",
    "unsupported_claim_count",
    "claim_count",
    "open_findings",
    "open_critical_findings",
    "stale_warning_count",
    "planner_gap_count",
    "analysis_run_count",
    "capsule_reproduction_status",
    "unobserved_checks",
    "failed_checks",
    "false_completion_candidate_count",
})

CRITERION_NAMES = frozenset({
    "anchored_to_record",
    "proof_review_present",
    "constraints_use_supported_claims",
    "expectation_keys_known",
    "expectations_met",
})


@dataclass(frozen=True)
class FalseCompletionObservation:
    """What was actually observed when the model said it was done.

    ``is_candidate`` marks a possible false completion (model claimed done but
    the local proof is not satisfied). This module only counts candidates;
    enforcing anything is explicitly out of scope until 0.4.13.
    """

    model_said_done: bool
    proof_status: str = ""
    satisfied: bool = False
    observed_checks: int = 0
    unobserved_checks: int = 0
    failed_checks: int = 0

    def is_candidate(self) -> bool:
        return bool(self.model_said_done) and not self.satisfied

    def to_payload(self) -> dict[str, object]:
        return {
            "model_said_done": bool(self.model_said_done),
            "proof_status": identifier(self.proof_status, 40),
            "satisfied": bool(self.satisfied),
            "observed_checks": max(0, int(self.observed_checks)),
            "unobserved_checks": max(0, int(self.unobserved_checks)),
            "failed_checks": max(0, int(self.failed_checks)),
        }


@dataclass(frozen=True)
class ResearchRegressionCriterion:
    name: str
    passed: bool


@dataclass(frozen=True)
class ResearchRegressionVerdict:
    ok: bool
    criteria: tuple[ResearchRegressionCriterion, ...]
    reason_codes: tuple[str, ...]

    def to_payload(self) -> dict[str, object]:
        return {
            "ok": bool(self.ok),
            "criteria": [
                {"name": identifier(item.name, 60), "passed": bool(item.passed)}
                for item in self.criteria
            ],
            "reason_codes": list(self.reason_codes[:MAX_REASON_CODES]),
        }


@dataclass(frozen=True)
class ResearchRegressionReport:
    report_id: str
    case_id: str
    record_ref: str
    metrics: Mapping[str, object]
    observables: Mapping[str, bool]
    verdict: ResearchRegressionVerdict
    false_completion: FalseCompletionObservation | None

    def observable(self, name: str) -> bool:
        return bool(self.observables.get(name, False))

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "report_id": self.report_id,
            "case_id": identifier(self.case_id, 80),
            "record_ref": self.record_ref,
            "metrics": dict(self.metrics),
            "observables": dict(self.observables),
            "verdict": self.verdict.to_payload(),
        }
        if self.false_completion is not None:
            payload["false_completion"] = self.false_completion.to_payload()
        return payload


@dataclass(frozen=True)
class ResearchRegressionInput:
    """One canonical argument bundle for :func:`build_regression_report`."""

    case_id: str = ""
    snapshot: object = None
    proof_review: object = None
    brief: object = None
    impact: object = None
    completion_proof: object = None
    model_said_done: bool = False
    findings: Iterable[object] = ()
    planner_gaps: Iterable[object] = ()
    capsule: object = None
    sources: Iterable[object] = ()
    relations: Iterable[object] = ()
    pipeline_payload: Mapping[str, object] | None = None
    expectations: Mapping[str, bool] | None = None


def observe_false_completion(
    model_said_done: object,
    completion_proof: object = None,
) -> FalseCompletionObservation:
    """Project one done-claim plus its local completion proof into counts."""

    payload = _payload_of(completion_proof) or {}
    checks_raw = payload.get("checks")
    rows = [item for item in checks_raw if isinstance(item, Mapping)] if isinstance(checks_raw, (list, tuple)) else []
    observed = sum(
        1 for row in rows if identifier(row.get("status"), 20) in {"pass", "fail"}
    )
    unobserved = sum(
        1 for row in rows if identifier(row.get("status"), 20) == "not_run"
    )
    failed = sum(1 for row in rows if identifier(row.get("status"), 20) == "fail")
    status = identifier(payload.get("status"), 40)
    return FalseCompletionObservation(
        model_said_done=bool(model_said_done),
        proof_status=status,
        satisfied=bool(payload.get("satisfied")),
        observed_checks=observed,
        unobserved_checks=unobserved,
        failed_checks=failed,
    )


def build_regression_report(
    input_bundle: ResearchRegressionInput | None = None,
    *,
    case_id: object = "",
    snapshot: object = None,
    proof_review: object = None,
    brief: object = None,
    impact: object = None,
    completion_proof: object = None,
    model_said_done: object = False,
    findings: Iterable[object] = (),
    planner_gaps: Iterable[object] = (),
    capsule: object = None,
    sources: Iterable[object] = (),
    relations: Iterable[object] = (),
    pipeline_payload: Mapping[str, object] | None = None,
    expectations: Mapping[str, bool] | None = None,
) -> ResearchRegressionReport | None:
    """Project one research round into a gated regression report.

    Returns None when neither snapshot nor brief anchors the report to a valid
    research record ref, so callers fail open without projecting half facts.
    """

    if isinstance(input_bundle, ResearchRegressionInput):
        return build_regression_report(
            case_id=input_bundle.case_id,
            snapshot=input_bundle.snapshot,
            proof_review=input_bundle.proof_review,
            brief=input_bundle.brief,
            impact=input_bundle.impact,
            completion_proof=input_bundle.completion_proof,
            model_said_done=input_bundle.model_said_done,
            findings=input_bundle.findings,
            planner_gaps=input_bundle.planner_gaps,
            capsule=input_bundle.capsule,
            sources=input_bundle.sources,
            relations=input_bundle.relations,
            pipeline_payload=input_bundle.pipeline_payload,
            expectations=input_bundle.expectations,
        )

    snapshot_payload = _payload_of(snapshot) or {}
    brief_payload = _payload_of(brief) or {}
    record_ref = str(snapshot_payload.get("record_ref") or brief_payload.get("record_ref") or "")
    if not record_ref:
        return None

    observation = observe_false_completion(model_said_done, completion_proof)
    claim_statuses = _claim_statuses(brief_payload)
    constraint_rows = _constraint_rows(_payload_of(impact))
    finding_rows = _finding_rows(findings)
    gap_rows = _gap_count(planner_gaps)
    capsule_status = _capsule_status(capsule)
    trust_stale_count, stale_trust = _source_stale_facts(sources)
    counter_relation = _counter_relation_present(relations)

    review = _review_facts(proof_review)
    pipeline = _pipeline_facts(pipeline_payload)

    grounded = sum(1 for status in claim_statuses.values() if status == "evidence_backed")
    unsupported = sum(1 for status in claim_statuses.values() if status == "unsupported")
    total_claims = len(claim_statuses)

    stale_source_flagged = bool(review["stale_warnings"]) or stale_trust or any(
        row["kind"] == FINDING_STALE_SOURCE for row in finding_rows
    )
    unsupported_in_constraints = any(
        not _constraint_supported(row, claim_statuses) for row in constraint_rows
    )
    conflicting_evidence = any(
        row["kind"] in {FINDING_CONTRADICTORY_SOURCES, FINDING_SOURCE_CONFLICT}
        for row in finding_rows
    ) or counter_relation

    metrics: dict[str, object] = {
        "answer_status": _answer_token(review["answer_status"] or brief_payload.get("answer_status")),
        "answer_coverage_score": review["coverage_score"],
        "grounded_ratio": round(grounded / total_claims, 3) if total_claims else 0.0,
        "grounded_claim_count": grounded,
        "unsupported_claim_count": unsupported,
        "claim_count": total_claims,
        "open_findings": sum(1 for row in finding_rows if row["status"] == STATUS_OPEN),
        "open_critical_findings": sum(
            1
            for row in finding_rows
            if row["status"] == STATUS_OPEN and row["severity"] == SEVERITY_CRITICAL
        ),
        "stale_warning_count": len(review["stale_warnings"]) + trust_stale_count,
        "planner_gap_count": gap_rows,
        "analysis_run_count": _analysis_run_count(snapshot_payload),
        "capsule_reproduction_status": capsule_status,
        "unobserved_checks": observation.unobserved_checks,
        "failed_checks": observation.failed_checks,
        "false_completion_candidate_count": 1 if observation.is_candidate() else 0,
    }
    observables: dict[str, bool] = {
        "answered": metrics["answer_status"] == "answered",
        "partial_answer": metrics["answer_status"] == "partial",
        "stale_source_flagged": stale_source_flagged,
        "unsupported_claim_present": unsupported > 0,
        "unsupported_in_constraints": unsupported_in_constraints,
        "counterevidence_checked": bool(review["counterevidence_checked"]),
        "citation_locator_verified": bool(review["citation_locator_verified"]),
        "support_relation_verified": bool(review["support_relation_verified"]),
        "ledger_record_verified": bool(review["ledger_record_verified"]),
        "overclaim_warned": bool(review["overclaim_warnings"]),
        "conflicting_evidence_finding": conflicting_evidence,
        "planner_gap_created": gap_rows > 0,
        "analysis_run_observed": int(metrics["analysis_run_count"]) > 0,
        "reproducible_analysis": capsule_status == "output_captured",
        "analysis_run_failed": capsule_status == "failed",
        "new_evidence_after_followup": pipeline["new_evidence"],
        "false_completion_candidate": observation.is_candidate(),
    }

    constraints_ok = all(
        _constraint_supported(row, claim_statuses) for row in constraint_rows
    )
    verdict = verdict_from_metrics(
        metrics,
        observables=observables,
        expectations=expectations,
        anchored=True,
        proof_reviewed=bool(proof_review is not None and review["present"]),
        constraints_verified=constraints_ok,
    )
    return ResearchRegressionReport(
        report_id=stable_ref(
            "research_regression",
            identifier(case_id, 80),
            record_ref,
            digest_json({"metrics": metrics, "observables": observables}),
        ),
        case_id=str(case_id or ""),
        record_ref=record_ref,
        metrics=metrics,
        observables=observables,
        verdict=verdict,
        false_completion=(
            observation if observation.model_said_done or observation.proof_status else None
        ),
    )


def verdict_from_metrics(
    metrics: Mapping[str, object],
    *,
    observables: Mapping[str, bool],
    expectations: Mapping[str, bool] | None,
    anchored: bool,
    proof_reviewed: bool,
    constraints_verified: bool,
) -> ResearchRegressionVerdict:
    """Derive the hard-gate verdict from metrics, observables, expectations.

    Criteria are gates, not scores:

    - unknown expectation keys fail closed;
    - every provided expectation must match the observed boolean exactly;
    - constraints may only cite claims the brief itself marked supported.
    """

    criteria: list[ResearchRegressionCriterion] = [
        ResearchRegressionCriterion("anchored_to_record", bool(anchored)),
        ResearchRegressionCriterion("proof_review_present", bool(proof_reviewed)),
        ResearchRegressionCriterion("constraints_use_supported_claims", bool(constraints_verified)),
    ]
    reasons: list[str] = []
    expectation_keys: list[str] = []
    for key in dict(expectations or {}):
        token = identifier(key, 60)
        if token:
            expectation_keys.append(token)
    unknown = [key for key in expectation_keys if key not in OBSERVABLE_NAMES]
    criteria.append(ResearchRegressionCriterion("expectation_keys_known", not unknown))
    if unknown:
        reasons.append("unknown_expectation_key")
    unmet: list[str] = []
    if not unknown:
        checked = dict(list((expectations or {}).items())[:MAX_EXPECTATION_KEYS])
        for key, expected in checked.items():
            token = identifier(key, 60)
            observed = bool(observables.get(token, False))
            if observed != bool(expected):
                unmet.append(token)
    criteria.append(ResearchRegressionCriterion("expectations_met", not unmet))
    for key in unmet:
        reasons.append(f"expectation_{key}_unmet")
    for criterion in criteria:
        if not criterion.passed:
            reasons.append(f"{criterion.name}_failed")
    deduped = tuple(dict.fromkeys(reasons))[:MAX_REASON_CODES]
    return ResearchRegressionVerdict(
        ok=all(item.passed for item in criteria),
        criteria=tuple(criteria),
        reason_codes=deduped,
    )


def _payload_of(item: object) -> Mapping[str, object] | None:
    if item is None:
        return None
    to_payload = getattr(item, "to_payload", None)
    if callable(to_payload):
        try:
            data = to_payload()
        except Exception:
            return None
        return data if isinstance(data, Mapping) else None
    if isinstance(item, Mapping):
        return item
    return None


def _review_facts(proof_review: object) -> dict[str, object]:
    payload = _payload_of(proof_review)
    if not payload:
        return {
            "present": False,
            "answer_status": "",
            "coverage_score": 0.0,
            "citation_locator_verified": False,
            "support_relation_verified": False,
            "counterevidence_checked": False,
            "ledger_record_verified": False,
            "stale_warnings": (),
            "overclaim_warnings": (),
        }
    warnings = payload.get("stale_warnings")
    overclaims = payload.get("overclaim_warnings")
    return {
        "present": True,
        "answer_status": identifier(payload.get("answer_status"), 40),
        "coverage_score": _bounded_score(payload.get("answer_coverage_score")),
        "citation_locator_verified": bool(payload.get("citation_locator_verified")),
        "support_relation_verified": bool(payload.get("support_relation_verified")),
        "counterevidence_checked": bool(payload.get("counterevidence_checked")),
        "ledger_record_verified": bool(payload.get("ledger_record_verified")),
        "stale_warnings": tuple(warnings)[:12] if isinstance(warnings, (list, tuple)) else (),
        "overclaim_warnings": (
            tuple(overclaims)[:12] if isinstance(overclaims, (list, tuple)) else ()
        ),
    }


def _pipeline_facts(pipeline_payload: Mapping[str, object] | None) -> dict[str, object]:
    payload = pipeline_payload if isinstance(pipeline_payload, Mapping) else {}
    fresh = nonnegative_int(payload.get("fresh_source_count"))
    new_evidence = nonnegative_int(payload.get("new_evidence_count"))
    return {"new_evidence": bool(fresh > 0 or new_evidence > 0)}


def _claim_statuses(brief_payload: Mapping[str, object]) -> dict[str, str]:
    rows = brief_payload.get("claims")
    out: dict[str, str] = {}
    if not isinstance(rows, (list, tuple)):
        return out
    for row in rows[:64]:
        if not isinstance(row, Mapping):
            continue
        ref = identifier(row.get("claim_ref"), 120)
        if not ref:
            continue
        status = identifier(row.get("status"), 40)
        out[ref] = status if status in CLAIM_STATUSES else "unsupported"
    return out


def _constraint_rows(impact_payload: Mapping[str, object] | None) -> list[dict[str, object]]:
    if not impact_payload:
        return []
    rows = impact_payload.get("implementation_constraints")
    out: list[dict[str, object]] = []
    if not isinstance(rows, (list, tuple)):
        return out
    for row in rows[:16]:
        if not isinstance(row, Mapping):
            continue
        refs = row.get("claim_refs")
        out.append({
            "support": identifier(row.get("support"), 20),
            "claim_refs": (
                [identifier(item, 120) for item in refs][:4]
                if isinstance(refs, (list, tuple))
                else []
            ),
        })
    return out


def _constraint_supported(
    row: Mapping[str, object],
    claim_statuses: Mapping[str, str],
) -> bool:
    if row.get("support") not in CONSTRAINT_SUPPORTS:
        return False
    # An implementation constraint may only cite claims the same brief marked
    # evidence-backed; unknown or uncertain refs fail closed.
    refs = row.get("claim_refs") or ()
    if not refs:
        return False
    return all(claim_statuses.get(ref) == "evidence_backed" for ref in refs)


def _finding_rows(findings: Iterable[object]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for item in findings or ():
        payload = _payload_of(item)
        if payload is None:
            continue
        out.append({
            "kind": identifier(payload.get("kind"), 40),
            "severity": identifier(payload.get("severity"), 20),
            "status": identifier(payload.get("status"), 20),
        })
        if len(out) >= MAX_FINDINGS_SCANNED:
            break
    return out


def _gap_count(planner_gaps: Iterable[object]) -> int:
    count = 0
    for item in planner_gaps or ():
        if _payload_of(item) is not None:
            count += 1
        if count >= MAX_FINDINGS_SCANNED:
            break
    return count


def _capsule_status(capsule: object) -> str:
    payload = _payload_of(capsule)
    if not payload:
        return ""
    status = identifier(payload.get("reproduction_status"), 40)
    return status if status in CAPSULE_STATUSES else ""


def _source_stale_facts(sources: Iterable[object]) -> tuple[int, bool]:
    rows = [item for item in (sources or ()) if item is not None][:MAX_SOURCES_PROJECTED]
    projections = project_source_set(rows)
    stale_count = sum(1 for projection in projections if projection.freshness == "stale")
    return stale_count, stale_count > 0


def _counter_relation_present(relations: Iterable[object]) -> bool:
    scanned = 0
    for item in (relations or ()):
        payload = item if isinstance(item, Mapping) else _payload_of(item)
        if isinstance(payload, Mapping):
            kind = identifier(payload.get("relation_kind"), 40)
            if kind in {"refutes", "limits"}:
                return True
        scanned += 1
        if scanned >= MAX_RELATIONS_SCANNED:
            break
    return False


def _analysis_run_count(snapshot_payload: Mapping[str, object]) -> int:
    refs = snapshot_payload.get("analysis_run_refs")
    counts = snapshot_payload.get("counts")
    if isinstance(counts, Mapping):
        return nonnegative_int(counts.get("analysis_runs"))
    if isinstance(refs, (list, tuple)):
        return len(refs)
    return 0


def _answer_token(value: object) -> str:
    text = identifier(value, 40).lower()
    return text if text in ANSWER_STATUSES else "not_answered"


def _bounded_score(value: object) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    if score < 0:
        return 0.0
    if score > 1:
        return 1.0
    return round(score, 3)


__all__ = [
    "CAPSULE_STATUSES",
    "CRITERION_NAMES",
    "FalseCompletionObservation",
    "METRIC_NAMES",
    "MAX_REASON_CODES",
    "OBSERVABLE_NAMES",
    "ResearchRegressionCriterion",
    "ResearchRegressionInput",
    "ResearchRegressionReport",
    "ResearchRegressionVerdict",
    "build_regression_report",
    "observe_false_completion",
    "verdict_from_metrics",
]
