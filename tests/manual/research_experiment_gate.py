"""Release-gate summary for Research experiment result JSON files.

The gate reads bounded manual result rows and projects only metrics: no raw
prompt, reply, transcript, webpage body, or report body is copied into its
output. It is meant to turn 0.4 Research experiment evidence into 0.5.7
promote-or-delete decisions.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from codey.research.followup_quality import followup_usefulness
from codey.research.source_finalizer_scoring import (
    aggregate_source_finalizer_rows,
    paired_source_finalizer_deltas,
)
from tests.manual.ab_harness_common import timestamp, write_json_atomic

PROBE = "research_experiment_gate"
RESULTS_DIR = Path(__file__).resolve().parent / "results"
DEFAULT_PATTERNS = (
    "bounded_research_planner_ab-*.json",
    "research_followup_quality_ab-*.json",
    "source_connector_ab-*.json",
    "source_connector_done_ab-*.json",
    "research_source_rendering_ab-*.json",
)
PROOF_GAP_CODES = (
    "claim_missing_citation",
    "claim_missing_evidence_ref",
    "claim_missing_support_relation",
    "claim_not_evidence_backed",
)
PROOF_GAP_PROBES = (
    "bounded_research_planner_ab",
    "research_followup_quality_ab",
    "source_connector_ab",
    "source_connector_done_ab",
    "research_source_rendering_ab",
    "research_source_wrapper_ab",
)


@dataclass(frozen=True)
class ResultPayload:
    path: Path
    payload: Mapping[str, Any]

    @property
    def probe(self) -> str:
        return str(self.payload.get("probe") or "").strip()

    @property
    def complete(self) -> bool:
        return self.payload.get("complete") is not False

    @property
    def rows(self) -> list[dict[str, Any]]:
        if not self.complete:
            return []
        rows = self.payload.get("rows")
        if not isinstance(rows, list):
            return []
        return [dict(row, _source_file=self.path.name) for row in rows if isinstance(row, Mapping)]


def default_result_paths(results_dir: Path = RESULTS_DIR) -> list[Path]:
    paths: list[Path] = []
    for pattern in DEFAULT_PATTERNS:
        paths.extend(Path(results_dir).glob(pattern))
    return _clean_result_paths(paths)


def expand_inputs(inputs: Sequence[str] | None, *, results_dir: Path = RESULTS_DIR) -> list[Path]:
    if not inputs:
        return default_result_paths(results_dir)
    paths: list[Path] = []
    for raw in inputs:
        value = str(raw or "").strip()
        if not value:
            continue
        candidate = Path(value)
        matches = sorted(candidate.parent.glob(candidate.name)) if any(ch in value for ch in "*?[") else [candidate]
        paths.extend(matches)
    return _clean_result_paths(paths)


def load_payloads(paths: Iterable[Path]) -> list[ResultPayload]:
    payloads: list[ResultPayload] = []
    for path in _clean_result_paths(paths):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, Mapping):
            payloads.append(ResultPayload(path=path, payload=payload))
    return payloads


def build_gate(payloads: Sequence[ResultPayload]) -> dict[str, Any]:
    complete_payloads = [item for item in payloads if item.complete]
    source_files = sorted({item.path.name for item in complete_payloads})
    skipped_incomplete_files = sorted({item.path.name for item in payloads if not item.complete})
    bounded = _bounded_followup_gate(complete_payloads)
    connectors = _source_connector_gate(complete_payloads)
    finalizer = _done_finalizer_gate(complete_payloads)
    source_wrapper = _source_wrapper_gate(complete_payloads)
    proof_gaps = _proof_gap_gate(complete_payloads)
    decisions = [
        _bounded_followup_decision(bounded),
        _source_connector_decision(connectors),
        _done_finalizer_decision(finalizer),
        _source_wrapper_decision(source_wrapper),
    ]
    return {
        "probe": PROBE,
        "generated_at": timestamp(),
        "source_file_count": len(source_files),
        "source_files": source_files[:120],
        "skipped_incomplete_files": skipped_incomplete_files[:120],
        "bounded_followup": bounded,
        "source_connectors": connectors,
        "done_finalizer": finalizer,
        "source_wrapper": source_wrapper,
        "proof_gaps": proof_gaps,
        "default_path_decisions": decisions,
        "verdict": {
            "ok": all(item["decision"] != "remove_default_path" for item in decisions),
            "reason_codes": [
                reason
                for item in decisions
                for reason in item.get("reason_codes", ())
                if isinstance(reason, str)
            ],
        },
    }


def _bounded_followup_gate(payloads: Sequence[ResultPayload]) -> dict[str, Any]:
    rows = [
        row
        for item in payloads
        if item.probe in {"bounded_research_planner_ab", "research_followup_quality_ab"}
        for row in item.rows
    ]
    pairs = []
    for baseline, planner in _paired_rows(
        rows,
        baseline_arm="baseline",
        treatment_arms=("planner",),
        include_source_file=True,
    ):
        usefulness = followup_usefulness(baseline, planner)
        pairs.append(
            {
                "provider": _text(planner.get("provider") or baseline.get("provider")),
                "case": _text(planner.get("case") or baseline.get("case")),
                "arm": "planner",
                "source_file": _text(planner.get("_source_file") or baseline.get("_source_file")),
                "mode": _text(planner.get("ab_followup_mode")),
                "evaluated": bool(usefulness.get("evaluated")),
                "useful": bool(usefulness.get("useful")),
                "quality_gain": bool(usefulness.get("quality_gain")),
                "quality_regression": bool(usefulness.get("quality_regression")),
                "followup_rounds": _int(planner.get("followup_rounds")),
                "planner_stop_reason": _text(planner.get("planner_stop_reason")),
                "score_delta": _int(usefulness.get("score_delta")),
                "coverage_delta": _float(usefulness.get("answer_coverage_delta")),
                "unsupported_claim_rate_delta": _float(usefulness.get("unsupported_claim_rate_delta")),
                "new_sources": _int(usefulness.get("new_sources")),
                "new_evidence": _int(usefulness.get("new_evidence")),
                "new_fetched_sources": _int(usefulness.get("new_fetched_sources")),
                "provider_send_delta": _int(usefulness.get("provider_send_delta")),
                "seconds_delta": _float(usefulness.get("seconds_delta")),
                "reason_codes": list(usefulness.get("reason_codes") or ()),
                "quality_regression_codes": list(usefulness.get("quality_regression_codes") or ()),
            }
        )
    latest_pairs = _latest_by_provider_case(pairs)
    return {
        "row_count": len(rows),
        "pair_count": len(pairs),
        "valid_pair_count": sum(1 for pair in pairs if pair["evaluated"]),
        "useful_pair_count": sum(1 for pair in pairs if pair["useful"]),
        "quality_regression_pair_count": sum(1 for pair in pairs if pair["quality_regression"]),
        "no_followup_pair_count": sum(1 for pair in pairs if pair["followup_rounds"] <= 0),
        "safe_evidence_only_pair_count": sum(1 for pair in pairs if _is_evidence_only_pair(pair)),
        "safe_evidence_only_useful_count": sum(
            1 for pair in pairs if _is_evidence_only_pair(pair) and pair["useful"]
        ),
        "latest_by_provider_case": latest_pairs,
        "by_provider": _bounded_by_provider(pairs),
        "sample_pairs": pairs[:80],
    }


def _source_connector_gate(payloads: Sequence[ResultPayload]) -> dict[str, Any]:
    rows = [row for item in payloads if item.probe == "source_connector_ab" for row in item.rows]
    pairs = []
    for baseline, connector in _paired_rows(
        rows,
        baseline_arm="baseline",
        treatment_arms=("connector",),
        include_source_file=True,
    ):
        pairs.append(
            {
                "provider": _text(connector.get("provider") or baseline.get("provider")),
                "case": _text(connector.get("case") or baseline.get("case")),
                "source_file": _text(connector.get("_source_file") or baseline.get("_source_file")),
                "baseline_opened_target_host": bool(baseline.get("opened_target_host")),
                "connector_opened_target_host": bool(connector.get("opened_target_host")),
                "target_host_delta": _bool_delta(connector.get("opened_target_host"), baseline.get("opened_target_host")),
                "score_delta": _int(connector.get("score")) - _int(baseline.get("score")),
                "evidence_delta": _int(connector.get("evidence_count")) - _int(baseline.get("evidence_count")),
                "baseline_stop_reason": _text(baseline.get("stop_reason")),
                "connector_stop_reason": _text(connector.get("stop_reason")),
                "baseline_proof_ok": bool(baseline.get("proof_ok")),
                "connector_proof_ok": bool(connector.get("proof_ok")),
            }
        )
    return {
        "row_count": len(rows),
        "pair_count": len(pairs),
        "target_host_gain_count": sum(1 for pair in pairs if pair["target_host_delta"] > 0),
        "target_host_loss_count": sum(1 for pair in pairs if pair["target_host_delta"] < 0),
        "score_gain_count": sum(1 for pair in pairs if pair["score_delta"] > 0),
        "score_loss_count": sum(1 for pair in pairs if pair["score_delta"] < 0),
        "proof_gain_count": sum(1 for pair in pairs if pair["connector_proof_ok"] and not pair["baseline_proof_ok"]),
        "by_provider": _connector_by_provider(pairs),
        "sample_pairs": pairs[:80],
    }


def _done_finalizer_gate(payloads: Sequence[ResultPayload]) -> dict[str, Any]:
    rows = [row for item in payloads if item.probe == "source_connector_done_ab" for row in item.rows]
    ok_rows = [row for row in rows if row.get("ok")]
    paired = []
    for baseline, treatment in _paired_rows(
        ok_rows,
        baseline_arm="baseline",
        treatment_arms=tuple(sorted({str(row.get("arm") or "") for row in ok_rows if row.get("arm") != "baseline"})),
        include_source_file=False,
    ):
        paired.append(
            {
                "provider": _text(treatment.get("provider") or baseline.get("provider")),
                "case": _text(treatment.get("case") or baseline.get("case")),
                "arm": _text(treatment.get("arm")),
                "source_file": _text(treatment.get("_source_file") or baseline.get("_source_file")),
                "score_delta": _int(treatment.get("score")) - _int(baseline.get("score")),
                "first_pass_delta": _bool_delta(treatment.get("first_done_passed"), baseline.get("first_done_passed")),
                "eventual_success_delta": _bool_delta(
                    treatment.get("eventual_done_passed"),
                    baseline.get("eventual_done_passed"),
                ),
                "done_attempt_delta": _int(treatment.get("done_attempts")) - _int(baseline.get("done_attempts")),
                "quality_retry_delta": _int(treatment.get("quality_retry_count"))
                - _int(baseline.get("quality_retry_count")),
                "connector_valid_delta": _bool_delta(treatment.get("connector_valid"), baseline.get("connector_valid")),
                "opened_target_delta": _bool_delta(
                    treatment.get("opened_target_host"),
                    baseline.get("opened_target_host"),
                ),
                "proof_ok_delta": _bool_delta(treatment.get("proof_ok"), baseline.get("proof_ok")),
                "baseline_stop_reason": _text(baseline.get("stop_reason")),
                "treatment_stop_reason": _text(treatment.get("stop_reason")),
            }
        )
    return {
        "row_count": len(rows),
        "ok_row_count": len(ok_rows),
        "aggregate": aggregate_source_finalizer_rows(ok_rows),
        "paired_by_arm": paired_source_finalizer_deltas(ok_rows),
        "paired_sample_count": len(paired),
        "first_pass_gain_count": sum(1 for pair in paired if pair["first_pass_delta"] > 0),
        "done_attempt_reduction_count": sum(1 for pair in paired if pair["done_attempt_delta"] < 0),
        "quality_retry_reduction_count": sum(1 for pair in paired if pair["quality_retry_delta"] < 0),
        "connector_loss_count": sum(1 for pair in paired if pair["connector_valid_delta"] < 0),
        "proof_gain_count": sum(1 for pair in paired if pair["proof_ok_delta"] > 0),
        "sample_pairs": paired[:80],
    }


def _source_wrapper_gate(payloads: Sequence[ResultPayload]) -> dict[str, Any]:
    rows = [
        row
        for item in payloads
        if item.probe in {"research_source_rendering_ab", "research_source_wrapper_ab"}
        for row in item.rows
    ]
    return {
        "row_count": len(rows),
        "injection_leak_count": sum(1 for row in rows if row.get("injection_tool_action_observed")),
        "quality_regression_count": sum(1 for row in rows if row.get("quality_regression")),
        "status": "no_live_ab_evidence" if not rows else "has_live_ab_evidence",
    }


def _proof_gap_gate(payloads: Sequence[ResultPayload]) -> dict[str, Any]:
    rows = [
        _proof_gap_row(row, probe=item.probe)
        for item in payloads
        if item.probe in PROOF_GAP_PROBES
        for row in item.rows
    ]
    return {
        "row_count": len(rows),
        "reviewed_row_count": sum(1 for row in rows if row["has_proof_review"]),
        "proof_failed_row_count": sum(1 for row in rows if row["proof_failed"]),
        "target_gap_row_count": sum(1 for row in rows if row["target_gaps"]),
        "target_gap_counts": _proof_gap_counts(rows),
        "by_probe": _proof_gap_groups(rows, "probe"),
        "by_provider": _proof_gap_groups(rows, "provider"),
        "sample_rows": [
            _proof_gap_sample(row)
            for row in rows
            if row["target_gaps"] or row["proof_failed"]
        ][:80],
    }


def _proof_gap_row(row: Mapping[str, Any], *, probe: str) -> dict[str, Any]:
    missing = _proof_missing_evidence(row)
    target_gaps = tuple(code for code in PROOF_GAP_CODES if code in missing)
    proof_ok = row.get("proof_ok")
    has_explicit_proof_ok = isinstance(proof_ok, bool)
    has_proof_review = bool(
        has_explicit_proof_ok
        or missing
        or _text(row.get("proof_answer_status"))
        or _text(row.get("proof_status"))
    )
    return {
        "probe": _text(probe),
        "provider": _text(row.get("provider")) or "<unknown>",
        "case": _text(row.get("case")) or "<unknown>",
        "arm": _text(row.get("arm")) or "<unknown>",
        "source_file": _text(row.get("_source_file")),
        "proof_ok": bool(proof_ok) if has_explicit_proof_ok else None,
        "proof_answer_status": _text(row.get("proof_answer_status")),
        "missing_evidence": tuple(missing),
        "target_gaps": target_gaps,
        "has_proof_review": has_proof_review,
        "proof_failed": (not bool(proof_ok)) if has_explicit_proof_ok else bool(missing),
    }


def _proof_gap_counts(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    return {
        code: sum(1 for row in rows if code in row.get("target_gaps", ()))
        for code in PROOF_GAP_CODES
    }


def _proof_gap_groups(rows: Sequence[Mapping[str, Any]], key: str) -> dict[str, Any]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[_text(row.get(key)) or "<unknown>"].append(row)
    return {
        name: _proof_gap_bucket(items)
        for name, items in sorted(grouped.items())
    }


def _proof_gap_bucket(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "rows": len(rows),
        "reviewed_rows": sum(1 for row in rows if row.get("has_proof_review")),
        "proof_failed_rows": sum(1 for row in rows if row.get("proof_failed")),
        "target_gap_rows": sum(1 for row in rows if row.get("target_gaps")),
        "target_gap_counts": _proof_gap_counts(rows),
    }


def _proof_gap_sample(row: Mapping[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "probe": _text(row.get("probe")),
        "provider": _text(row.get("provider")),
        "case": _text(row.get("case")),
        "arm": _text(row.get("arm")),
        "source_file": _text(row.get("source_file")),
        "proof_answer_status": _text(row.get("proof_answer_status")),
        "target_gaps": list(row.get("target_gaps", ())),
    }
    if row.get("proof_ok") is not None:
        payload["proof_ok"] = bool(row.get("proof_ok"))
    return payload


def _bounded_followup_decision(summary: Mapping[str, Any]) -> dict[str, Any]:
    safe_count = _int(summary.get("safe_evidence_only_pair_count"))
    useful_count = _int(summary.get("safe_evidence_only_useful_count"))
    regression_count = sum(
        1
        for pair in summary.get("latest_by_provider_case", ())
        if isinstance(pair, Mapping) and pair.get("quality_regression")
    )
    if useful_count >= 3 and regression_count == 0:
        decision = "keep_default_narrow_evidence_followup"
        reason_codes = ["evidence_only_followup_has_cross_provider_useful_pairs"]
    elif useful_count:
        decision = "keep_default_with_more_live_gate"
        reason_codes = ["evidence_only_followup_has_signal_but_gate_is_small"]
    else:
        decision = "manual_only_until_quality_win"
        reason_codes = ["no_stable_followup_quality_win"]
    return {
        "feature": "bounded evidence-only follow-up",
        "decision": decision,
        "reason_codes": reason_codes,
        "plain": (
            "Keep the evidence-only follow-up that writes evidence and lets code merge it; do not restore the old full-report rewrite shape."
            if useful_count
            else "Do not enable follow-up by default until quality evidence exists."
        ),
        "supporting_counts": {
            "safe_evidence_only_pairs": safe_count,
            "useful_pairs": useful_count,
            "latest_quality_regressions": regression_count,
        },
    }


def _source_connector_decision(summary: Mapping[str, Any]) -> dict[str, Any]:
    gain = _int(summary.get("target_host_gain_count"))
    loss = _int(summary.get("target_host_loss_count"))
    if gain > loss:
        decision = "keep_default_for_source_reach"
        reason_codes = ["connector_improves_target_source_reach"]
    else:
        decision = "manual_only_until_source_reach_win"
        reason_codes = ["connector_source_reach_win_not_clear"]
    return {
        "feature": "PubMed/arXiv source connector",
        "decision": decision,
        "reason_codes": reason_codes,
        "plain": "Keep the connector for source reach; it gets Codey to better sources but does not by itself prove the final report is perfect.",
        "supporting_counts": {
            "target_host_gains": gain,
            "target_host_losses": loss,
            "score_gains": _int(summary.get("score_gain_count")),
            "score_losses": _int(summary.get("score_loss_count")),
        },
    }


def _done_finalizer_decision(summary: Mapping[str, Any]) -> dict[str, Any]:
    first_pass = _int(summary.get("first_pass_gain_count"))
    fewer_done = _int(summary.get("done_attempt_reduction_count"))
    connector_losses = _int(summary.get("connector_loss_count"))
    if first_pass or fewer_done:
        decision = "keep_narrow_done_finalizer"
        reason_codes = ["done_finalizer_reduces_repair_loops"]
    else:
        decision = "manual_only_until_done_stage_win"
        reason_codes = ["done_stage_win_not_clear"]
    if connector_losses:
        reason_codes.append("some_treatments_lost_connector_validity")
    return {
        "feature": "done citation/source finalizer",
        "decision": decision,
        "reason_codes": reason_codes,
        "plain": "Keep the narrow citation/source finalizer; do not describe it as a research-quality improver or promote the old batch/checklist arms.",
        "supporting_counts": {
            "first_pass_gains": first_pass,
            "done_attempt_reductions": fewer_done,
            "quality_retry_reductions": _int(summary.get("quality_retry_reduction_count")),
            "proof_gains": _int(summary.get("proof_gain_count")),
            "connector_losses": connector_losses,
        },
    }


def _source_wrapper_decision(summary: Mapping[str, Any]) -> dict[str, Any]:
    rows = _int(summary.get("row_count"))
    leaks = _int(summary.get("injection_leak_count"))
    regressions = _int(summary.get("quality_regression_count"))
    if rows and leaks == 0 and regressions == 0:
        decision = "eligible_to_promote_after_live_review"
        reason_codes = ["source_wrapper_has_no_recorded_injection_or_quality_regression"]
    else:
        decision = "do_not_promote_without_ab"
        reason_codes = ["source_wrapper_has_no_live_ab_evidence"] if not rows else ["source_wrapper_gate_failed"]
    return {
        "feature": "untrusted source wrapper",
        "decision": decision,
        "reason_codes": reason_codes,
        "plain": "Do not promote it yet; first prove malicious source text cannot become model actions and normal evidence extraction does not regress.",
        "supporting_counts": {
            "rows": rows,
            "injection_leaks": leaks,
            "quality_regressions": regressions,
        },
    }


def _paired_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    baseline_arm: str,
    treatment_arms: Sequence[str],
    include_source_file: bool,
) -> list[tuple[Mapping[str, Any], Mapping[str, Any]]]:
    grouped: dict[tuple[str, str, int, str], dict[str, Mapping[str, Any]]] = defaultdict(dict)
    for row in rows:
        if row.get("error"):
            continue
        arm = _text(row.get("arm"))
        if arm not in {baseline_arm, *treatment_arms}:
            continue
        key = (
            _text(row.get("provider")).lower(),
            _text(row.get("case")),
            max(1, _int(row.get("sample") or row.get("repeat") or 1)),
            _text(row.get("_source_file")) if include_source_file else "",
        )
        if not key[0] or not key[1]:
            continue
        grouped[key][arm] = row
    pairs: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    for arms in grouped.values():
        baseline = arms.get(baseline_arm)
        if baseline is None:
            continue
        for arm in treatment_arms:
            treatment = arms.get(arm)
            if treatment is not None:
                pairs.append((baseline, treatment))
    return pairs


def _bounded_by_provider(pairs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for pair in pairs:
        grouped[_text(pair.get("provider")) or "<unknown>"].append(pair)
    return {
        provider: {
            "pairs": len(items),
            "useful": sum(1 for item in items if item.get("useful")),
            "quality_regressions": sum(1 for item in items if item.get("quality_regression")),
            "no_followup": sum(1 for item in items if _int(item.get("followup_rounds")) <= 0),
        }
        for provider, items in sorted(grouped.items())
    }


def _connector_by_provider(pairs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for pair in pairs:
        grouped[_text(pair.get("provider")) or "<unknown>"].append(pair)
    return {
        provider: {
            "pairs": len(items),
            "target_host_gains": sum(1 for item in items if _int(item.get("target_host_delta")) > 0),
            "target_host_losses": sum(1 for item in items if _int(item.get("target_host_delta")) < 0),
            "score_gains": sum(1 for item in items if _int(item.get("score_delta")) > 0),
            "score_losses": sum(1 for item in items if _int(item.get("score_delta")) < 0),
        }
        for provider, items in sorted(grouped.items())
    }


def _latest_by_provider_case(pairs: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[tuple[str, str], Mapping[str, Any]] = {}
    for pair in pairs:
        key = (_text(pair.get("provider")).lower(), _text(pair.get("case")))
        if not key[0] or not key[1]:
            continue
        latest[key] = pair
    return [dict(item) for _key, item in sorted(latest.items())]


def _is_evidence_only_pair(pair: Mapping[str, Any]) -> bool:
    mode = _text(pair.get("mode"))
    return bool(
        mode in {
            "connector_backed_evidence_followup",
            "evidence_only_patch_merge",
            "production_evidence_followup",
        }
        and _int(pair.get("followup_rounds")) > 0
        and _int(pair.get("new_evidence")) > 0
    )


def _proof_missing_evidence(row: Mapping[str, Any]) -> tuple[str, ...]:
    value = row.get("proof_missing_evidence")
    if not isinstance(value, (list, tuple)):
        review = row.get("proof_review")
        value = review.get("missing_evidence") if isinstance(review, Mapping) else ()
    codes: list[str] = []
    for item in value if isinstance(value, (list, tuple)) else ():
        code = _text(item)
        if code and code not in codes:
            codes.append(code)
    return tuple(codes)


def _clean_result_paths(paths: Iterable[Path]) -> list[Path]:
    clean: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        candidate = Path(path)
        if not candidate.is_file() or candidate.name.endswith(".trace.json"):
            continue
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        clean.append(candidate)
    return sorted(clean, key=lambda item: item.as_posix())


def _bool_delta(new: object, old: object) -> int:
    return int(bool(new)) - int(bool(old))


def _text(value: object) -> str:
    return str(value or "").strip()


def _int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (OverflowError, TypeError, ValueError):
        return 0


def _float(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return round(parsed, 3) if math.isfinite(parsed) else 0.0


def _self_test() -> None:
    payloads = [
        ResultPayload(
            path=Path("bounded.json"),
            payload={
                "probe": "bounded_research_planner_ab",
                "rows": [
                    {
                        "provider": "mimo",
                        "case": "pubmed",
                        "arm": "baseline",
                        "ok": True,
                        "score": 5,
                        "record_source_count": 1,
                        "record_evidence_count": 1,
                        "unsupported_claim_rate": 0.3,
                        "provider_send_count": 5,
                    },
                    {
                        "provider": "mimo",
                        "case": "pubmed",
                        "arm": "planner",
                        "ok": True,
                        "score": 6,
                        "record_source_count": 2,
                        "record_evidence_count": 2,
                        "unsupported_claim_rate": 0.2,
                        "provider_send_count": 6,
                        "followup_rounds": 1,
                        "ab_followup_mode": "production_evidence_followup",
                        "proof_coverage": 0.7,
                    },
                ],
            },
        ),
        ResultPayload(
            path=Path("connector.json"),
            payload={
                "probe": "source_connector_ab",
                "rows": [
                    {"provider": "mimo", "case": "pubmed", "arm": "baseline", "ok": True, "score": 2},
                    {
                        "provider": "mimo",
                        "case": "pubmed",
                        "arm": "connector",
                        "ok": True,
                        "score": 5,
                        "opened_target_host": True,
                    },
                ],
            },
        ),
        ResultPayload(
            path=Path("done.json"),
            payload={
                "probe": "source_connector_done_ab",
                "rows": [
                    {
                        "provider": "mimo",
                        "case": "pubmed",
                        "arm": "baseline",
                        "ok": True,
                        "done_attempts": 2,
                        "quality_retry_count": 1,
                        "eventual_done_passed": True,
                    },
                    {
                        "provider": "mimo",
                        "case": "pubmed",
                        "arm": "boundary",
                        "ok": True,
                        "done_attempts": 1,
                        "quality_retry_count": 0,
                        "first_done_passed": True,
                        "eventual_done_passed": True,
                    },
                ],
            },
        ),
    ]
    gate = build_gate(payloads)
    decisions = {item["feature"]: item["decision"] for item in gate["default_path_decisions"]}
    assert decisions["bounded evidence-only follow-up"] == "keep_default_with_more_live_gate"
    assert decisions["PubMed/arXiv source connector"] == "keep_default_for_source_reach"
    assert decisions["done citation/source finalizer"] == "keep_narrow_done_finalizer"
    assert decisions["untrusted source wrapper"] == "do_not_promote_without_ab"


def main() -> int:
    parser = argparse.ArgumentParser(description="Score Research experiment result JSON for 0.5.7 release decisions")
    parser.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    parser.add_argument("--input", action="append", help="result JSON path or glob; defaults to known Research result files")
    parser.add_argument("--output", type=Path, help="write bounded gate summary JSON")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        _self_test()
        print("self-test ok")
        return 0

    paths = expand_inputs(args.input, results_dir=args.results_dir)
    gate = build_gate(load_payloads(paths))
    text = json.dumps(gate, ensure_ascii=False, indent=2)
    if args.output:
        write_json_atomic(args.output, gate)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
