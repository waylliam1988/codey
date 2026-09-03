"""Offline projection for bounded planner narrow merge results.

This probe reuses saved manual A/B JSON and trace files. It does not replay a
live provider and it does not claim to rebuild the full Research ledger. The
goal is narrower: estimate whether a deterministic evidence-only final report
projection would have improved paired A/B usefulness for rows where the trace
already contains fresh `knowledge_write` evidence.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from codey.research.followup_quality import followup_usefulness, score_followup_quality_row
from codey.research.protocols import extract_json_objects


RESULTS_DIR = Path(__file__).resolve().parent / "results"
DEFAULT_INPUTS = (
    RESULTS_DIR / "bounded_research_planner_ab-deepseek-evidenceonly3-paired-widget-20260821.json",
    RESULTS_DIR / "bounded_research_planner_ab-mimo-evidenceonly3-paired-widget-20260821.json",
    RESULTS_DIR / "bounded_research_planner_ab-qwen-evidenceonly3-paired-widget-20260821.json",
    RESULTS_DIR / "bounded_research_planner_ab-stepfun-evidenceonly3-paired-widget-20260820.json",
    RESULTS_DIR / "bounded_research_planner_ab-glm-evidenceonly3-paired-widget-20260820.json",
    RESULTS_DIR / "bounded_research_planner_ab-deepseek-production-20260821.json",
    RESULTS_DIR / "bounded_research_planner_ab-qwen-production-20260821.json",
    RESULTS_DIR / "bounded_research_planner_ab-stepfun-production-20260821.json",
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", action="append", type=Path, dest="inputs")
    parser.add_argument("--output", type=Path, default=RESULTS_DIR / "bounded_research_merge_projection-20260821.json")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        _self_test()
        print("self-test ok")
        return 0
    inputs = tuple(args.inputs or DEFAULT_INPUTS)
    payload = run_projection(inputs)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for row in payload["rows"]:
        observed = row["observed"]
        projected = row["projected"]
        print(
            f"[{row['provider']} {row['phase']} {row['case']}] "
            f"observed={observed['score']}/{observed['useful']} "
            f"projected={projected['score']}/{projected['useful']} "
            f"fresh={row['fresh_source_count']}/{row['fresh_evidence_count']} "
            f"reason={projected['projection_reason']}",
            flush=True,
        )
    print(f"wrote {args.output}")
    return 0


def run_projection(inputs: tuple[Path, ...]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for path in inputs:
        if not path.exists():
            rows.append({
                "input": str(path),
                "provider": _provider_from_name(path.name),
                "phase": _phase_from_name(path.name),
                "case": "",
                "error": "missing_input",
            })
            continue
        payload = _read_json(path)
        trace = _read_json(_trace_path(path))
        case_rows = _pair_rows(payload.get("rows") or [])
        for case, pair in sorted(case_rows.items()):
            baseline = pair.get("baseline") or {}
            planner = pair.get("planner") or {}
            observed = _observed_summary(baseline, planner)
            fresh = _fresh_evidence_from_trace(trace, baseline=baseline, planner=planner)
            projected_planner = _project_planner_row(
                baseline=baseline,
                planner=planner,
                fresh=fresh,
            )
            projected = _projected_summary(baseline, planner, projected_planner)
            rows.append({
                "input": str(path),
                "trace": str(_trace_path(path)),
                "provider": str(payload.get("provider") or _provider_from_name(path.name)),
                "phase": _phase_from_name(path.name),
                "case": case,
                "fresh_source_count": len(fresh["source_urls"]),
                "fresh_evidence_count": len(fresh["evidence"]),
                "fresh_source_urls": fresh["source_urls"],
                "observed": observed,
                "projected": projected,
            })
    return {
        "kind": "bounded_research_merge_projection",
        "note": (
            "Offline projection only: saved A/B rows do not contain full ledger "
            "or ResearchRecord payloads."
        ),
        "rows": rows,
        "summary": _projection_summary(rows),
    }


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _trace_path(path: Path) -> Path:
    return path.with_name(path.name.removesuffix(".json") + ".trace.json")


def _pair_rows(rows: list[Any]) -> dict[str, dict[str, dict[str, Any]]]:
    pairs: dict[str, dict[str, dict[str, Any]]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        case = str(row.get("case") or "unknown")
        arm = str(row.get("arm") or "")
        if arm not in {"baseline", "planner"}:
            continue
        pairs.setdefault(case, {})[arm] = row
    return pairs


def _observed_summary(baseline: dict[str, Any], planner: dict[str, Any]) -> dict[str, Any]:
    usefulness = followup_usefulness(baseline, planner)
    return {
        "score": int(planner.get("score") or 0),
        "useful": bool(usefulness.get("useful")),
        "coverage": _float(planner.get("proof_coverage")),
        "unsupported_claim_rate": _float(planner.get("unsupported_claim_rate")),
        "sources": int(planner.get("record_source_count") or 0),
        "evidence": int(planner.get("record_evidence_count") or 0),
        "claims": int(planner.get("record_claim_count") or 0),
        "unsupported_claims": int(planner.get("unsupported_claim_count") or 0),
        "followup_rounds": int(planner.get("followup_rounds") or 0),
        "planner_stop_reason": str(planner.get("planner_stop_reason") or ""),
        "usefulness": usefulness,
    }


def _projected_summary(
    baseline: dict[str, Any],
    planner: dict[str, Any],
    projected_planner: dict[str, Any],
) -> dict[str, Any]:
    usefulness = followup_usefulness(baseline, projected_planner)
    return {
        "score": int(projected_planner.get("score") or 0),
        "useful": bool(usefulness.get("useful")),
        "coverage": _float(projected_planner.get("proof_coverage")),
        "unsupported_claim_rate": _float(projected_planner.get("unsupported_claim_rate")),
        "sources": int(projected_planner.get("record_source_count") or 0),
        "evidence": int(projected_planner.get("record_evidence_count") or 0),
        "claims": int(projected_planner.get("record_claim_count") or 0),
        "unsupported_claims": int(projected_planner.get("unsupported_claim_count") or 0),
        "followup_rounds": int(projected_planner.get("followup_rounds") or 0),
        "planner_stop_reason": str(projected_planner.get("planner_stop_reason") or ""),
        "projection_reason": str(projected_planner.get("projection_reason") or ""),
        "usefulness": usefulness,
        "observed_score": int(planner.get("score") or 0),
    }


def _fresh_evidence_from_trace(
    trace: dict[str, Any],
    *,
    baseline: dict[str, Any],
    planner: dict[str, Any],
) -> dict[str, Any]:
    baseline_fetches = {str(url or "") for url in baseline.get("fixture_fetches") or ()}
    planner_fetches = {str(url or "") for url in planner.get("fixture_fetches") or ()}
    fresh_urls = {url for url in planner_fetches - baseline_fetches if url}
    rows: list[dict[str, str]] = []
    source_urls: set[str] = set()
    seen: set[tuple[str, str]] = set()
    for event in trace.get("events") or ():
        if not isinstance(event, dict) or event.get("arm") != "planner" or event.get("event") != "reply":
            continue
        for obj in extract_json_objects(str(event.get("reply") or "")):
            if str(obj.get("tool") or "").strip() != "knowledge_write":
                continue
            args = obj.get("args")
            if not isinstance(args, dict):
                continue
            evidence_raw = args.get("evidence")
            evidence_items = evidence_raw if isinstance(evidence_raw, list) else [evidence_raw]
            for item in evidence_items:
                if not isinstance(item, dict):
                    continue
                source_url = str(item.get("source_url") or item.get("source") or "").strip()
                excerpt = _single_line(item.get("excerpt"))
                claim = _single_line(item.get("claim") or excerpt)
                if fresh_urls and source_url not in fresh_urls:
                    continue
                if not source_url or not excerpt:
                    continue
                key = (source_url, excerpt)
                if key in seen:
                    continue
                seen.add(key)
                source_urls.add(source_url)
                rows.append({
                    "source_url": source_url,
                    "excerpt": excerpt,
                    "claim": claim,
                    "stance": str(item.get("stance") or "supports"),
                })
    return {
        "source_urls": sorted(source_urls),
        "evidence": rows,
    }


def _project_planner_row(
    *,
    baseline: dict[str, Any],
    planner: dict[str, Any],
    fresh: dict[str, Any],
) -> dict[str, Any]:
    projected = dict(planner)
    fresh_sources = len(fresh["source_urls"])
    fresh_evidence = len(fresh["evidence"])
    if fresh_evidence <= 0:
        projected["projection_reason"] = "no_fresh_trace_evidence"
        return projected
    baseline_sources = int(baseline.get("record_source_count") or 0)
    baseline_evidence = int(baseline.get("record_evidence_count") or 0)
    projected_sources = max(int(planner.get("record_source_count") or 0), baseline_sources + fresh_sources)
    projected_evidence = max(int(planner.get("record_evidence_count") or 0), baseline_evidence + fresh_evidence)
    projected_claims = max(1, projected_evidence)
    projected["record_source_count"] = projected_sources
    projected["record_evidence_count"] = projected_evidence
    projected["record_claim_count"] = projected_claims
    projected["unsupported_claim_count"] = 0
    projected["unsupported_claim_rate"] = 0.0
    projected["evidence_count"] = projected_evidence
    projected["proof_answer_status"] = _project_answer_status(baseline, planner, fresh_evidence)
    projected["proof_coverage"] = _project_coverage(baseline, planner, fresh_evidence)
    projected["expected_terms_present"] = bool(
        planner.get("expected_terms_present")
        or baseline.get("expected_terms_present")
        or _fresh_text_has_expected_terms(fresh)
    )
    projected["followup_rounds"] = max(1, int(planner.get("followup_rounds") or 0))
    projected["followup_applied"] = True
    projected["planner_stop_reason"] = "projected_narrow_evidence_merge"
    projected["pipeline_stop_reason"] = "done"
    projected["stop_reason"] = "done"
    projected["proof_missing_evidence"] = _project_missing_evidence(planner)
    projected["score"] = score_followup_quality_row(projected)
    projected["projection_reason"] = "fresh_trace_evidence_narrow_merge"
    return projected


def _project_answer_status(
    baseline: dict[str, Any],
    planner: dict[str, Any],
    fresh_evidence: int,
) -> str:
    ranks = {"answered": 3, "partial": 2, "insufficient_evidence": 1, "not_answered": 0}
    observed = str(planner.get("proof_answer_status") or "")
    base = str(baseline.get("proof_answer_status") or "")
    best = observed if ranks.get(observed, 0) >= ranks.get(base, 0) else base
    if ranks.get(best, 0) >= 2:
        return best
    return "partial" if fresh_evidence > 0 else best or "not_answered"


def _project_coverage(
    baseline: dict[str, Any],
    planner: dict[str, Any],
    fresh_evidence: int,
) -> float:
    observed = max(_float(baseline.get("proof_coverage")), _float(planner.get("proof_coverage")))
    if fresh_evidence > 0:
        observed = max(observed, 0.556)
    return round(observed, 3)


def _project_missing_evidence(planner: dict[str, Any]) -> list[str]:
    removed = {
        "unsupported_claims",
        "claim_not_evidence_backed",
        "claim_missing_evidence_ref",
        "claim_missing_support_relation",
        "not_answered",
    }
    rows = [
        str(item)
        for item in planner.get("proof_missing_evidence") or ()
        if str(item) not in removed
    ]
    if not rows:
        rows = ["partial_answer", "counterevidence_not_checked"]
    return rows[:8]


def _fresh_text_has_expected_terms(fresh: dict[str, Any]) -> bool:
    text = " ".join(
        f"{item.get('claim', '')} {item.get('excerpt', '')}"
        for item in fresh.get("evidence") or ()
        if isinstance(item, dict)
    ).casefold()
    return "stable-v2" in text or "recommended endpoint" in text


def _projection_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    valid = [row for row in rows if not row.get("error")]
    improved = [
        row for row in valid
        if not row["observed"]["useful"] and row["projected"]["useful"]
    ]
    retained = [
        row for row in valid
        if row["observed"]["useful"] and row["projected"]["useful"]
    ]
    regressed = [
        row for row in valid
        if row["observed"]["useful"] and not row["projected"]["useful"]
    ]
    return {
        "rows": len(valid),
        "observed_useful": sum(1 for row in valid if row["observed"]["useful"]),
        "projected_useful": sum(1 for row in valid if row["projected"]["useful"]),
        "converted_to_useful": [f"{row['provider']}:{row['phase']}:{row['case']}" for row in improved],
        "retained_useful": [f"{row['provider']}:{row['phase']}:{row['case']}" for row in retained],
        "regressed_from_useful": [f"{row['provider']}:{row['phase']}:{row['case']}" for row in regressed],
    }


def _float(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _clip(value: object, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "..."


def _single_line(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _provider_from_name(name: str) -> str:
    match = re.match(r"bounded_research_planner_ab-([^-]+)-", name)
    return match.group(1) if match else ""


def _phase_from_name(name: str) -> str:
    if "production" in name:
        return "production"
    if "evidenceonly3" in name:
        return "evidenceonly3"
    return "unknown"


def _self_test() -> None:
    baseline = {
        "ok": True,
        "arm": "baseline",
        "score": 5,
        "proof_answer_status": "partial",
        "proof_coverage": 0.556,
        "unsupported_claim_rate": 0.333,
        "record_source_count": 1,
        "record_evidence_count": 1,
        "record_claim_count": 3,
        "unsupported_claim_count": 1,
        "fixture_fetches": ["https://source-a.test/widget-storage"],
        "provider_send_count": 5,
        "seconds": 10.0,
        "expected_terms_present": True,
    }
    planner = {
        "ok": True,
        "arm": "planner",
        "score": 6,
        "proof_answer_status": "partial",
        "proof_coverage": 0.667,
        "unsupported_claim_rate": 0.75,
        "record_source_count": 2,
        "record_evidence_count": 2,
        "record_claim_count": 4,
        "unsupported_claim_count": 3,
        "fixture_fetches": [
            "https://source-a.test/widget-storage",
            "https://source-b.test/widget-storage-update",
        ],
        "provider_send_count": 6,
        "seconds": 12.0,
        "expected_terms_present": True,
        "followup_rounds": 1,
    }
    fresh = {
        "source_urls": ["https://source-b.test/widget-storage-update"],
        "evidence": [{
            "source_url": "https://source-b.test/widget-storage-update",
            "excerpt": "stable-v2 remains the recommended endpoint.",
            "claim": "stable-v2 remains the recommended endpoint.",
            "stance": "supports",
        }],
    }
    projected = _project_planner_row(baseline=baseline, planner=planner, fresh=fresh)
    useful = followup_usefulness(baseline, projected)
    assert projected["unsupported_claim_count"] == 0
    assert projected["record_claim_count"] == 2
    assert useful["useful"] is True
    no_projection = _project_planner_row(baseline=baseline, planner=planner, fresh={"source_urls": [], "evidence": []})
    assert no_projection["projection_reason"] == "no_fresh_trace_evidence"


if __name__ == "__main__":
    raise SystemExit(main())
