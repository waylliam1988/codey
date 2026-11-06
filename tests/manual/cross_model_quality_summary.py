"""Paired artifact-quality accounting; no workflow/Review verdict substitution."""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from tests.manual.cross_model_quality_ab import artifact_passed, summarize


def compare(rows, candidate, baseline="baseline", *, production_plan=None):
    indexed = {(row["repeat"], row["case"], row["arm"]): row for row in rows}
    if len(indexed) != len(rows):
        raise ValueError("duplicate experiment identity; cannot count the same run twice")
    pairs = []
    for row in rows:
        if row["arm"] != candidate:
            continue
        if production_plan is not None and row["production_stage_applicable"] != production_plan:
            continue
        other = indexed.get((row["repeat"], row["case"], baseline))
        if other is None:
            continue
        a, b = artifact_passed(row["final"]), artifact_passed(other["final"])
        pairs.append({"case": row["case"], "repeat": row["repeat"],
                      "candidate_passed": a, "baseline_passed": b,
                      "candidate_behavior_score": row["final"]["quality_score"],
                      "baseline_behavior_score": other["final"]["quality_score"],
                      "candidate_treatment_delivered": row["treatment_delivered"],
                      "outcome": "win" if a and not b else "loss" if b and not a else "tie"})
    counts = Counter(pair["outcome"] for pair in pairs)
    return {"candidate": candidate, "baseline": baseline, "count": len(pairs),
            "candidate_passed": sum(pair["candidate_passed"] for pair in pairs),
            "baseline_passed": sum(pair["baseline_passed"] for pair in pairs),
            "wins": counts["win"], "losses": counts["loss"], "ties": counts["tie"], "pairs": pairs}


def report(payload):
    rows = payload["rows"]
    comparisons = {
        "plan_greenfield": compare(rows, "plan", production_plan=True),
        "plan_existing_diagnostic": compare(rows, "plan", production_plan=False),
        "audit": compare(rows, "audit"),
        "consensus_one": compare(rows, "consensus_one"),
        "consensus_two": compare(rows, "consensus_two"),
        "second_advisor_increment": compare(rows, "consensus_two", "consensus_one"),
    }
    repaired = []
    for row in rows:
        if "repair" not in row:
            continue
        initial, final = row["initial"]["quality"], row["final"]
        repaired.append({"case": row["case"], "repeat": row["repeat"], "arm": row["arm"],
                         "initial_passed": artifact_passed(initial), "final_passed": artifact_passed(final),
                         "initial_behavior_score": initial["quality_score"],
                         "final_behavior_score": final["quality_score"],
                         "initial_scope_ok": initial["scope_ok"], "final_scope_ok": final["scope_ok"]})
    return {
        "planned": len(payload["metadata"]["schedule"]), "observed": len(rows),
        "matrix_complete": len(rows) == len(payload["metadata"]["schedule"]),
        "source_unchanged": payload.get("source_unchanged"),
        "summary": summarize(rows), "comparisons": comparisons, "repairs": repaired,
        "failures": [{"case": row["case"], "arm": row["arm"], "repeat": row["repeat"],
                      "stop_reason": row.get("final_stop_reason"), "failure_stage": row.get("failure_stage"),
                      "error": row.get("error"), "artifact_quality_passed": artifact_passed(row["final"]),
                      "failed_behavior_checks": [check["name"] for check in row["final"]["quality"] if not check["ok"]],
                      "visible_suite_ok": row["final"]["visible_suite"]["ok"], "scope_ok": row["final"]["scope_ok"]}
                     for row in rows if not artifact_passed(row["final"]) or "error" in row],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = report(json.loads(args.input.read_text(encoding="utf-8")))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k not in ("comparisons", "failures", "repairs")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
