"""Sequential local-model matrices; preserve every failure for manual grading.

All cells use the same production kernel and local submission contract. The
factorial matrix changes relationship splitting and history projection only;
the current work packet, source snapshot, ordering and sampling stay fixed.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path


def matrix(suite: str) -> list[dict]:
    cases = ("versions", "checkpoint_constraints") if suite == "factorial" else (
        "writer_limit", "readonly_conditions", "checkpoint_stop", "checkpoint_resume", "coexistence_only")
    configurations = (("A", False, True), ("B", True, True), ("C", False, False), ("D", True, False)) if suite == "factorial" else (("local", False, False),)
    return [{"case": case, "seed": seed, "order_seed": 0, "configuration": label,
             "split": split, "full_history": full_history,
             "layer": "candidates" if suite == "candidates" else "given"}
            for label, split, full_history in configurations for case in cases for seed in (74, 75, 76)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--source-file", type=Path, required=True)
    parser.add_argument("--suite", choices=("minimal", "factorial", "candidates"), required=True)
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=False)
    runs = matrix(args.suite)
    index = {"suite": args.suite, "semantic_grading": "manual_required", "runs": runs}
    path = args.directory / "matrix.json"
    for row in runs:
        name = f"{row['configuration']}-{row['case']}-{row['seed']}"
        directory = args.directory / name
        command = [sys.executable, "-X", "utf8", "-m", "tests.manual.research_evidence_local",
            "--directory", str(directory), "--source-file", str(args.source_file),
            "--case", row["case"], "--layer", row["layer"], "--seed", str(row["seed"]),
            "--order-seed", str(row["order_seed"]), "--max-turns", "12", "--provider-timeout", "120",
            *(["--split"] if row["split"] else []), *(["--full-history"] if row["full_history"] else [])]
        row["directory"] = str(directory.resolve())
        path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
        started = time.monotonic()
        print(f"START {name}", flush=True)
        with (args.directory / f"{name}.log").open("w", encoding="utf-8") as log:
            try:
                process = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=900)
                row["process_exit_code"] = process.returncode
            except subprocess.TimeoutExpired:
                row["runner_timeout"] = True
        row["elapsed_seconds"] = round(time.monotonic() - started, 2)
        result = directory / "research-result.json"
        if result.is_file():
            row["result"] = json.loads(result.read_text(encoding="utf-8"))
        row["semantic_grade"] = "pending_manual_review"
        path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"END {name}: {row.get('result', {}).get('stop_reason', 'runner_failure')} ({row['elapsed_seconds']}s)", flush=True)


if __name__ == "__main__":
    main()
