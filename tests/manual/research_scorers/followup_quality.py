"""Pure A/B scoring helpers for Research follow-up quality."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping


def score_followup_quality_row(row: Mapping[str, object]) -> int:
    status_score = {
        "answered": 4,
        "partial": 2,
        "insufficient_evidence": 1,
        "not_answered": 0,
    }.get(str(row.get("proof_answer_status") or ""), 0)
    return (
        (4 if row.get("proof_ok") else 0)
        + status_score
        + min(3, _nonnegative_int(row.get("evidence_count")))
        + (2 if row.get("expected_terms_present") else 0)
    )


def followup_usefulness(
    baseline: Mapping[str, object],
    planner: Mapping[str, object],
) -> dict[str, object]:
    if not baseline or not planner:
        return {"evaluated": False}
    baseline_ok = bool(baseline.get("ok"))
    planner_ok = bool(planner.get("ok"))
    if not baseline_ok or not planner_ok:
        return {
            "evaluated": False,
            "reason": "row_not_ok",
            "baseline_ok": baseline_ok,
            "planner_ok": planner_ok,
        }
    coverage_delta = round(_float(planner.get("proof_coverage")) - _float(baseline.get("proof_coverage")), 3)
    unsupported_rate_delta = round(
        _float(planner.get("unsupported_claim_rate")) - _float(baseline.get("unsupported_claim_rate")),
        3,
    )
    score_delta = _int(planner.get("score")) - _int(baseline.get("score"))
    source_delta = _nonnegative_int(planner.get("record_source_count")) - _nonnegative_int(
        baseline.get("record_source_count")
    )
    evidence_delta = _nonnegative_int(planner.get("record_evidence_count")) - _nonnegative_int(
        baseline.get("record_evidence_count")
    )
    query_delta = _length(planner.get("fixture_queries")) - _length(baseline.get("fixture_queries"))
    fetch_delta = _length(planner.get("fixture_fetches")) - _length(baseline.get("fixture_fetches"))
    baseline_fetch_urls = {str(item or "") for item in _sequence(baseline.get("fixture_fetches"))}
    planner_fetch_urls = {str(item or "") for item in _sequence(planner.get("fixture_fetches"))}
    new_fetched_urls = tuple(sorted(url for url in planner_fetch_urls - baseline_fetch_urls if url))
    send_delta = _int(planner.get("provider_send_count")) - _int(baseline.get("provider_send_count"))
    seconds_delta = round(_float(planner.get("seconds")) - _float(baseline.get("seconds")), 3)
    answer_status_delta = answer_status_rank(planner.get("proof_answer_status")) - answer_status_rank(
        baseline.get("proof_answer_status")
    )
    reasons: list[str] = []
    quality_reasons: list[str] = []
    quality_regressions: list[str] = []
    if coverage_delta >= 0.05:
        reasons.append("coverage_improved")
        quality_reasons.append("coverage_improved")
    elif coverage_delta <= -0.05:
        quality_regressions.append("coverage_regressed")
    if unsupported_rate_delta <= -0.02:
        reasons.append("unsupported_rate_improved")
        quality_reasons.append("unsupported_rate_improved")
    elif unsupported_rate_delta >= 0.02:
        quality_regressions.append("unsupported_rate_regressed")
    if evidence_delta > 0:
        reasons.append("new_evidence")
    if source_delta > 0:
        reasons.append("new_sources")
    if new_fetched_urls:
        reasons.append("new_fetched_sources")
    if answer_status_delta > 0:
        reasons.append("answer_status_improved")
        quality_reasons.append("answer_status_improved")
    elif answer_status_delta < 0:
        quality_regressions.append("answer_status_regressed")
    if planner.get("proof_ok") and not baseline.get("proof_ok"):
        reasons.append("proof_ok_recovered")
        quality_reasons.append("proof_ok_recovered")
    elif baseline.get("proof_ok") and not planner.get("proof_ok"):
        quality_regressions.append("proof_ok_regressed")
    if score_delta > 0:
        reasons.append("score_improved")
    elif score_delta < 0:
        quality_regressions.append("score_regressed")
    if planner.get("expected_terms_present") and not baseline.get("expected_terms_present"):
        reasons.append("expected_terms_recovered")
        quality_reasons.append("expected_terms_recovered")
    elif baseline.get("expected_terms_present") and not planner.get("expected_terms_present"):
        quality_regressions.append("expected_terms_lost")
    material_gain = bool(source_delta > 0 or evidence_delta > 0)
    execution_material_gain = bool(new_fetched_urls)
    quality_gain = bool(quality_reasons)
    quality_regression = bool(quality_regressions)
    useful = bool(
        _nonnegative_int(planner.get("followup_rounds")) > 0
        and material_gain
        and quality_gain
        and not quality_regression
    )
    return {
        "evaluated": True,
        "useful": useful,
        "material_gain": material_gain,
        "execution_material_gain": execution_material_gain,
        "quality_gain": quality_gain,
        "quality_regression": quality_regression,
        "reason_codes": reasons,
        "quality_regression_codes": quality_regressions,
        "followup_rounds": _nonnegative_int(planner.get("followup_rounds")),
        "new_sources": max(0, source_delta),
        "new_evidence": max(0, evidence_delta),
        "new_fetched_sources": len(new_fetched_urls),
        "new_fetched_source_urls": [_clip(url, 160) for url in new_fetched_urls[:6]],
        "answer_coverage_delta": coverage_delta,
        "unsupported_claim_rate_delta": unsupported_rate_delta,
        "answer_status_delta": answer_status_delta,
        "score_delta": score_delta,
        "query_delta": query_delta,
        "fetch_delta": fetch_delta,
        "provider_send_delta": send_delta,
        "seconds_delta": seconds_delta,
    }


def answer_status_rank(status: object) -> int:
    return {
        "answered": 3,
        "partial": 2,
        "insufficient_evidence": 1,
        "not_answered": 0,
    }.get(str(status or ""), 0)


def _float(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return parsed if math.isfinite(parsed) else 0.0


def _int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (OverflowError, TypeError, ValueError):
        return 0


def _nonnegative_int(value: object) -> int:
    return max(0, _int(value))


def _length(value: object) -> int:
    return len(_sequence(value))


def _sequence(value: object) -> tuple[object, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Mapping):
        return ()
    if isinstance(value, Iterable):
        return tuple(value)
    return ()


def _clip(value: object, limit: int) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if limit <= 3:
        return text[:limit]
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


__all__ = [
    "answer_status_rank",
    "followup_usefulness",
    "score_followup_quality_row",
]
