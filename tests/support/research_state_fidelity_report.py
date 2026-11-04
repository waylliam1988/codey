"""Export reviewed live trials, including failed and interrupted runs, for Git."""

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path

from tests.support.research_revision_longread_audit import audit
from tests.support.research_trial_audit import knowledge_versions, read_json, read_lines


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def export_phase(root):
    spec = read_json(root / "preregistration.json")
    expected = {(item["case"], seed, arm) for item in spec["matrix"]
                for seed in spec["seeds"] for arm in item["arms"]}
    actual = {(row["case"], row["seed"], row["arm"]) for row in read_json(root / "matrix.json")}
    if actual != expected:
        raise ValueError("Unfinished or unexpected matrix: " + root.name)
    for name, value in read_json(root / "frozen-inputs.json").items():
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != value:
            raise ValueError("Frozen input changed: " + name)
    result = audit(root)
    groups = defaultdict(list)
    fingerprints = {}
    for row in result["reviewed_trials"]:
        review = row["body_review"]
        if not review or review["body_sha256"] != row["body_sha256"]:
            raise ValueError("Missing or stale independent review: " + row["name"])
        groups[row["case"], row["arm"]].append(row)
    for trial in read_json(root / "matrix.json"):
        name = f"{trial['case']}-{trial['seed']}-{trial['arm']}"
        fingerprints[name] = [{
            "event": event["event"], "index": event.get("index"),
            **({"payload_sha256": digest(event["payload"]),
                "sampling": {k: event["payload"].get(k) for k in (
                    "model", "seed", "temperature", "top_p", "max_tokens", "tool_choice", "parallel_tool_calls")}}
               if event["event"] == "admitted_payload" else
               {"response_sha256": digest(event.get("response_text")), "backend_perf": event.get("backend_perf"),
                "elapsed_seconds": event.get("elapsed_seconds")}),
        } for event in read_lines(root / "wire" / (name + ".jsonl"))
            if event["event"] in {"admitted_payload", "generation_response"}]
    result["groups"] = [{
        "case": case, "arm": arm, "complete_trials": len(rows),
        "delivered": sum(row["delivery"] for row in rows),
        "minimum_body_pass": sum(row["body_review"]["minimum_body_pass"] for row in rows),
        "full_task_pass": sum(row["body_review"]["full_task_pass"] for row in rows),
        "requests": sum(row["requests"] for row in rows),
        "input_tokens": sum(row["input_tokens"] for row in rows),
        "output_tokens": sum(row["output_tokens"] for row in rows),
    } for (case, arm), rows in groups.items()]
    paired = []
    for item in spec["matrix"]:
        case, arms = item["case"], item["arms"]
        control = "frozen_control" if "frozen_control" in arms else "baseline"
        original = {row["seed"]: row for row in groups.get((case, control), [])}
        for arm in arms:
            if arm == control:
                continue
            candidate = {row["seed"]: row for row in groups.get((case, arm), [])}
            seeds = sorted(original.keys() & candidate.keys())
            before, after = ([rows[seed] for seed in seeds] for rows in (original, candidate))
            input_before = sum(row["input_tokens"] for row in before)
            input_after = sum(row["input_tokens"] for row in after)
            paired.append({"case": case, "control": control, "candidate": arm, "complete_paired_seeds": seeds,
                "control_pass": sum(row["body_review"]["full_task_pass"] for row in before),
                "candidate_pass": sum(row["body_review"]["full_task_pass"] for row in after),
                "control_requests": sum(row["requests"] for row in before),
                "candidate_requests": sum(row["requests"] for row in after),
                "control_input_tokens": input_before, "candidate_input_tokens": input_after,
                "input_change_percent": round(100 * (input_after / input_before - 1), 2) if input_before else None})
    result["paired_comparisons"] = paired
    result["request_fingerprints"] = fingerprints
    files = [*root.glob("*-candidate.py"), *root.glob("*-dependency.py")]
    result["frozen_files"] = {p.name: p.read_text(encoding="utf-8") for p in files}
    result["metadata"] = {name: read_json(root / name) for name in (
        "frozen-inputs.json", "production-sha256.json", "backend-model.json", "seed-identities.json",
        "limits.json", "material-channel.json", "overflow-source-budget.json", "determinism-check.json",
        "original-runtime-sha256.json") if (root / name).exists()}
    result["backend_interruptions"] = [read_json(p) for p in sorted(root.glob("backend-interruption-*.json"))]
    result["backend_identity_checks"] = [read_json(p) for p in sorted(root.glob("backend-restart*-identity.json"))]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("roots", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    roots = [path.resolve() for path in args.roots]
    phases = [export_phase(path) for path in roots]
    first = roots[0]
    payload = {
        "description": "Real 12B calls; source/body/call/database reviews. Saving alone is not a passing task.",
        "limitations": "Exploratory fixed cases and seeds; no population-level capability or cost claim. "
                       "Raw wire remains in local .e2e-artifacts; this export includes fixtures, exact calls, bodies, "
                       "database versions, source identities, backend accounting and request fingerprints.",
        "fixtures": read_json(first / "fixtures.json"),
        "initial_knowledge": {name: knowledge_versions(first / name) for name in (
            "warm-knowledge", "empty-knowledge", "revision-knowledge", "conflict-knowledge", "inference-knowledge")},
        "phases": phases,
        "attempted_trials": sum(len(phase["reviewed_trials"]) + len(phase["interrupted_trials"]) for phase in phases),
        "complete_trials": sum(len(phase["reviewed_trials"]) for phase in phases),
        "interrupted_trials": sum(len(phase["interrupted_trials"]) for phase in phases),
    }
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Exported", payload["attempted_trials"], "attempts,", payload["interrupted_trials"], "interruptions")


if __name__ == "__main__":
    main()
