"""Paired artifact-quality accounting; no workflow/Review verdict substitution."""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from tests.manual.cross_model_quality_ab import artifact_passed, summarize


def quality_delta(before, after):
    old = {check["name"]: check["ok"] for check in before["quality"]}
    new = {check["name"]: check["ok"] for check in after["quality"]}
    if old.keys() != new.keys():
        raise ValueError("quality comparison requires the same independent checks")
    fixed = [name for name in old if not old[name] and new[name]]
    regressed = [name for name in old if old[name] and not new[name]]
    suite_fixed = not before["visible_suite"]["ok"] and after["visible_suite"]["ok"]
    suite_regressed = before["visible_suite"]["ok"] and not after["visible_suite"]["ok"]
    scope_fixed = not before["scope_ok"] and after["scope_ok"]
    scope_regressed = before["scope_ok"] and not after["scope_ok"]
    gain, loss = bool(fixed or suite_fixed or scope_fixed), bool(regressed or suite_regressed or scope_regressed)
    return {"fixed_behavior_checks": fixed, "regressed_behavior_checks": regressed,
            "suite_fixed": suite_fixed, "suite_regressed": suite_regressed,
            "scope_fixed": scope_fixed, "scope_regressed": scope_regressed,
            "verified_gain_without_regression": gain and not loss,
            "outcome": "mixed" if gain and loss else "benefit" if gain else "regression" if loss else "tie"}


def compare(rows, candidate, baseline="baseline", *, production_plan=None, baseline_stage="final"):
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
        before = (other["final"] if baseline_stage == "final" else
                  other.get("initial", {}).get("quality", other["final"]))
        a, b = artifact_passed(row["final"]), artifact_passed(before)
        pairs.append({"case": row["case"], "repeat": row["repeat"],
                      "candidate_passed": a, "baseline_passed": b,
                      "candidate_behavior_score": row["final"]["quality_score"],
                      "baseline_behavior_score": before["quality_score"],
                      "baseline_initial_available": "initial" in other,
                      "candidate_treatment_delivered": row["treatment_delivered"],
                      "outcome": "win" if a and not b else "loss" if b and not a else "tie"})
        if "quality" in before and "quality" in row["final"]:
            pairs[-1]["checked_quality_delta"] = quality_delta(before, row["final"])
    counts = Counter(pair["outcome"] for pair in pairs)
    deltas = Counter(pair["checked_quality_delta"]["outcome"] for pair in pairs if "checked_quality_delta" in pair)
    return {"candidate": candidate, "baseline": baseline, "baseline_stage": baseline_stage, "count": len(pairs),
            "candidate_passed": sum(pair["candidate_passed"] for pair in pairs),
            "baseline_passed": sum(pair["baseline_passed"] for pair in pairs),
            "wins": counts["win"], "losses": counts["loss"], "ties": counts["tie"],
            "verified_benefits": deltas["benefit"], "checked_regressions": deltas["regression"],
            "mixed_changes": deltas["mixed"], "checked_ties": deltas["tie"], "pairs": pairs}


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
    cooperation = {arm: compare(rows, arm, baseline_stage="initial")
                   for arm in ("baseline", "plan", "audit", "consensus_one", "consensus_two")}
    repaired = []
    for row in rows:
        if not row.get("repair_attempted", "repair" in row or row.get("failure_stage") == "repair"):
            continue
        initial, final = row["initial"]["quality"], row["final"]
        repaired.append({"case": row["case"], "repeat": row["repeat"], "arm": row["arm"],
                         "repair_execution_failed": row.get("failure_stage") == "repair",
                         "initial_passed": artifact_passed(initial), "final_passed": artifact_passed(final),
                         "initial_behavior_score": initial["quality_score"],
                         "final_behavior_score": final["quality_score"],
                         "initial_scope_ok": initial["scope_ok"], "final_scope_ok": final["scope_ok"]})
        repaired[-1]["checked_quality_delta"] = quality_delta(initial, final)
    return {
        "planned": len(payload["metadata"]["schedule"]), "observed": len(rows),
        "matrix_complete": len(rows) == len(payload["metadata"]["schedule"]),
        "source_unchanged": payload.get("source_unchanged"),
        "summary": summarize(rows), "comparisons": comparisons,
        "cooperation_vs_local_writer_only": cooperation, "repairs": repaired,
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
    print(json.dumps({k: v for k, v in result.items() if k not in (
        "comparisons", "cooperation_vs_local_writer_only", "failures", "repairs",
    )}, ensure_ascii=False))


if __name__ == "__main__":
    main()
