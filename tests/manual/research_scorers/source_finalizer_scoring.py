"""Pure scoring helpers for Research source-finalizer A/B rows."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence


def score_source_finalizer_row(row: Mapping[str, object]) -> int:
    return (
        (3 if row.get("stop_reason") == "done" else 0)
        + (3 if row.get("opened_target_host") else 0)
        + (2 if _int(row.get("evidence_count")) > 0 else 0)
        + (2 if row.get("proof_ok") else 0)
        + (1 if row.get("expected_terms_present") else 0)
    )


def aggregate_source_finalizer_rows(rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    return {
        "count": len(rows),
        "first_pass_rate": rate_rows(rows, "first_done_passed"),
        "eventual_success_rate": rate_rows(rows, "eventual_done_passed"),
        "clean_success_rate": rate_rows(rows, "clean_success"),
        "proof_pass_rate": rate_rows(rows, "proof_ok"),
        "connector_valid_rate": rate_rows(rows, "connector_valid"),
        "average_done_attempts": average_rows(rows, "done_attempts"),
        "average_quality_retry_count": average_rows(rows, "quality_retry_count"),
        "average_score": average_rows(rows, "score"),
    }


def paired_source_finalizer_deltas(rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    by_sample: dict[int, dict[str, Mapping[str, object]]] = {}
    for row in rows:
        sample = _int(row.get("sample")) or 1
        arm = str(row.get("arm") or "")
        if not arm:
            continue
        by_sample.setdefault(sample, {})[arm] = row
    arms = sorted({str(row.get("arm") or "") for row in rows if row.get("arm") and row.get("arm") != "baseline"})
    summary: dict[str, object] = {}
    for arm in arms:
        pairs = [(items.get("baseline"), items.get(arm)) for items in by_sample.values()]
        complete_pairs = [(base_row, arm_row) for base_row, arm_row in pairs if base_row and arm_row]
        if not complete_pairs:
            continue
        baseline_rate = rate_rows([base_row for base_row, _arm_row in complete_pairs], "first_done_passed") or 0.0
        arm_rate = rate_rows([arm_row for _base_row, arm_row in complete_pairs], "first_done_passed") or 0.0
        summary[arm] = {
            "paired_samples": len(complete_pairs),
            "score_delta_avg": average_deltas(complete_pairs, "score"),
            "done_attempt_delta_avg": average_deltas(complete_pairs, "done_attempts"),
            "quality_retry_delta_avg": average_deltas(complete_pairs, "quality_retry_count"),
            "first_pass_delta": round(arm_rate - baseline_rate, 3),
        }
    return summary


def average_deltas(
    pairs: Sequence[tuple[Mapping[str, object], Mapping[str, object]]],
    key: str,
) -> float | None:
    deltas: list[float] = []
    for base_row, arm_row in pairs:
        base_value = _float_or_none(base_row.get(key))
        arm_value = _float_or_none(arm_row.get(key))
        if base_value is None or arm_value is None:
            continue
        deltas.append(arm_value - base_value)
    if not deltas:
        return None
    return round(sum(deltas) / len(deltas), 3)


def rate_rows(rows: Sequence[Mapping[str, object]], key: str) -> float | None:
    if not rows:
        return None
    return round(sum(1 for row in rows if row.get(key)) / len(rows), 3)


def average_rows(rows: Sequence[Mapping[str, object]], key: str) -> float | None:
    values: list[float] = []
    for row in rows:
        value = row.get(key)
        if isinstance(value, bool) or value in (None, ""):
            continue
        parsed = _float_or_none(value)
        if parsed is not None:
            values.append(parsed)
    if not values:
        return None
    return round(sum(values) / len(values), 3)


def _int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (OverflowError, TypeError, ValueError):
        return 0


def _float_or_none(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


__all__ = [
    "aggregate_source_finalizer_rows",
    "average_deltas",
    "average_rows",
    "paired_source_finalizer_deltas",
    "rate_rows",
    "score_source_finalizer_row",
]
