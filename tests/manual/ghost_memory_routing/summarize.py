"""Export reply-free metrics, or recount the bundled experiment snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path

from experiment_paths import DATA_ROOT, SCRIPT_DIR

EXPERIMENTS = {
    "runs": "same_prompt_sorting",
    "runs2": "expanded_slot_sorting",
    "runs3": "tiered_routing_ablation",
    "runs4": "family_budget_lifecycle_acceptance",
}
METRIC_KEYS = frozenset({
    "arm", "cell", "suite", "model", "rep", "index", "task", "task_family",
    "judge_passed", "judge_judge", "chars", "seconds", "stop_reason", "approval_denied",
})
SNAPSHOT = SCRIPT_DIR / "reports" / "measurements.json"


def export_metrics(destination: Path) -> None:
    experiments = {}
    for directory, experiment in EXPERIMENTS.items():
        rows = []
        sources = {}
        for path in sorted((DATA_ROOT / directory).glob("*.jsonl")):
            content = path.read_bytes()
            sources[path.name] = hashlib.sha256(content).hexdigest()
            for line in content.decode("utf-8").splitlines():
                if line.strip():
                    raw = json.loads(line)
                    row = {key: value for key, value in raw.items() if key in METRIC_KEYS}
                    if "text" in row or "judge_detail" in row:
                        raise AssertionError("Replies and excerpts must stay local")
                    rows.append(row)
        if not rows:
            raise ValueError(f"No measurements in {DATA_ROOT / directory}")
        experiments[experiment] = {"source_sha256": sources, "rows": rows}
    payload = {
        "schema_version": 1,
        "description": "Historical metrics only; legacy scores are not production promotion gates.",
        "capture_code_revision": None,
        "experiments": experiments,
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def summarize(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    result = {}
    for experiment, data in payload["experiments"].items():
        groups = defaultdict(lambda: {"runs": 0, "passed": 0, "chars_sum": 0})
        for row in data["rows"]:
            family = row.get("task_family", row.get("task", "")) or "override"
            key = " / ".join((row.get("suite", "legacy"), row.get("cell", row.get("arm", "")), family))
            cell = groups[key]
            cell["runs"] += 1
            cell["passed"] += int(bool(row.get("judge_passed")))
            cell["chars_sum"] += row.get("chars", 0)
        result[experiment] = {"runs": len(data["rows"]), "cells": dict(groups)}
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=SNAPSHOT)
    parser.add_argument("--export", action="store_true", help="Export local raw-run metrics to --input")
    args = parser.parse_args()
    if args.export:
        export_metrics(args.input)
    print(json.dumps(summarize(args.input), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
