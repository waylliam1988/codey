"""Export only bounded metrics from an intact baseline/candidate smoke archive."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from experiment_paths import DATA_ROOT


def summarize(root: Path, *, candidate_attempt: str = "") -> dict:
    manifests = {}
    metrics = {}
    for phase in ("baseline", "candidate"):
        directory = root / (f"candidate-{candidate_attempt}" if phase == "candidate" and candidate_attempt else phase)
        if (directory / "failure.json").exists():
            raise ValueError(f"{phase} contains a failure; no complete smoke summary")
        manifests[phase] = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        manifest = manifests[phase]
        if manifest["cells"] != ["on", "off"] or manifest["tasks"] != 1 or manifest["reps"] != 1:
            raise ValueError("unexpected matrix")
        for cell in ("on", "off"):
            row = json.loads((directory / cell / "metrics.json").read_text(encoding="utf-8"))
            if row["cell"] != cell or row["stop_reason"] != "done" or row["requests"] != 1:
                raise ValueError(f"{phase}/{cell} is incomplete")
            metrics[phase, cell] = row
    config_keys = ("suite", "endpoint", "model", "context", "reserve", "keep_recent", "output_tokens",
                   "temperature", "thinking_enabled", "native_tools", "timeout_seconds", "max_attempts",
                   "tasks", "cells", "reps", "production_entry", "profile", "policy_grants", "max_turns")
    if any(manifests["baseline"][key] != manifests["candidate"][key] for key in config_keys):
        raise ValueError("baseline and candidate configuration mismatch")
    comparisons = []
    for cell in ("on", "off"):
        before, after = metrics["baseline", cell], metrics["candidate", cell]
        if before["hint_present"] != (cell == "on") or after["hint_present"] != (cell == "on"):
            raise ValueError("hint placement mismatch")
        comparisons.append({
            "cell": cell, "baseline": before, "candidate": after,
            "prompt_identical": before["prompt_sha256"] == after["prompt_sha256"],
            "request_identical": before["request_sha256"] == after["request_sha256"],
        })
    return {
        "schema": "ghost_scope_authority_smoke_metrics_v1",
        "config": {key: manifests["baseline"][key] for key in config_keys},
        "completed": 4, "expected": 4, "comparisons": comparisons,
        "limits": ["structural smoke only", "one model, one task, one repetition per cell",
                   "no language efficacy or latency improvement claim", "provider usage unavailable; no zero imputation"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=DATA_ROOT / "scope_authority_20261021")
    parser.add_argument("--candidate-attempt", default="")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.candidate_attempt and (Path(args.candidate_attempt).name != args.candidate_attempt
                                   or args.candidate_attempt in {".", ".."}):
        parser.error("candidate attempt must be a directory name")
    result = summarize(args.archive, candidate_attempt=args.candidate_attempt)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"completed": result["completed"], "expected": result["expected"],
                      "all_requests_identical": all(row["request_identical"] for row in result["comparisons"])}))


if __name__ == "__main__":
    main()
