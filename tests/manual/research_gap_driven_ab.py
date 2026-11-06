"""Isolated A/B for gap driven next action guidance.

The candidate only appends one short instruction to the existing model facing
Research guidance.  It does not add a planner, tool, reviewer, or publication
path.  Use the existing frozen Research worker and score the resulting runs
with an independent rubric.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen
from uuid import uuid4

from tests.manual import research_branch_review as branch
from tests.manual import research_framework_comparison as framework
from tests.manual.research_experiment_identity import freeze_experiment, verify_experiment

ARMS = ("baseline", "gap_directive")
GAP_DIRECTIVE = (
    "Choose the next search or read to resolve one specific uncertainty that could change the answer to the "
    "original task. When a conclusion depends on a disputed or conditional premise, look for the relevant "
    "primary-source limitation or counterexample before spending budget on more general supporting summaries."
)


@contextmanager
def treatment(arm: str):
    """Patch only the model-facing guidance inside an isolated worker."""

    if arm not in ARMS:
        raise ValueError(arm)
    if arm == "baseline":
        yield
        return
    from codey.research import answer_context, answer_contract

    guidance = answer_contract.ANSWER_GUIDANCE + "\n" + GAP_DIRECTIVE
    with patch.object(answer_contract, "ANSWER_GUIDANCE", guidance), patch.object(
        answer_context, "ANSWER_GUIDANCE", guidance
    ):
        yield


def worker(root: Path, directory: Path, base_url: str, case: str, seed: int, arm: str) -> None:
    """Run the normal isolated worker with the treatment selected in-process."""

    with patch.object(branch, "ablation", treatment):
        branch.worker(root, directory, base_url, case, seed, arm)


def run_one(root: Path, case: str, seed: int, arm: str) -> dict[str, object]:
    verify_experiment(root)
    name = f"{case}-{seed}-{arm}"
    directory = root / "runs" / name
    limits = framework.experiment_limits(root)
    started = time.monotonic()
    with framework.InferenceProxy(
        root / "wire" / f"{name}.jsonl", seed, genkey="gap-driven-" + uuid4().hex,
        request_limit=limits["requests"], identity_root=root,
    ) as proxy, (root / "logs" / f"{name}.stdout").open("w", encoding="utf-8") as out, \
            (root / "logs" / f"{name}.stderr").open("w", encoding="utf-8") as err:
        process = subprocess.Popen(
            [sys.executable, "-X", "utf8", "-m", __spec__.name, "worker", "--root", str(root),
             "--directory", str(directory), "--base-url", proxy, "--case", case, "--seed", str(seed),
             "--arm", arm], stdout=out, stderr=err,
        )
        timed_out = False
        try:
            process.communicate(timeout=limits["process_seconds"])
        except subprocess.TimeoutExpired:
            timed_out = True
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
            process.communicate(timeout=15)
    row: dict[str, object] = {"case": case, "seed": seed, "arm": arm,
                              "process_exit_code": process.returncode, "runner_timeout": timed_out,
                              "elapsed_seconds": round(time.monotonic() - started, 2),
                              "semantic_grade": "pending_independent_review"}
    result = directory / "research-result.json"
    if result.exists():
        row["result"] = framework.read_json(result)
    framework.write_json(root / "results" / f"{name}.json", row)
    verify_experiment(root)
    return row


def freeze(root: Path) -> None:
    for folder in ("runs", "wire", "logs", "results"):
        (root / folder).mkdir(exist_ok=True)
    framework.backend_ready()
    with urlopen(framework.UPSTREAM + "/v1/models", timeout=5) as response:
        framework.write_json(root / "backend-model.json", json.load(response))
    names = ["preregistration.json", "backend-model.json", "seed-identities.json", "clock-context.txt"]
    for folder in ("warm-knowledge", "empty-knowledge"):
        names.extend(str(p.relative_to(root)).replace("\\", "/") for p in sorted((root / folder).rglob("*")) if p.is_file())
    framework.write_json(root / "frozen-inputs.json", {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in names
    })
    freeze_experiment(root)
    verify_experiment(root)


def run_matrix(root: Path, *, case: str | None = None, seed: int | None = None, arm: str | None = None) -> None:
    verify_experiment(root)
    spec = framework.read_json(root / "preregistration.json")
    rows = framework.read_json(root / "matrix.json") if (root / "matrix.json").exists() else []
    for index, item in enumerate(spec["matrix"]):
        if case and case != item["case"]:
            continue
        for current_seed in spec["seeds"]:
            if seed is not None and seed != current_seed:
                continue
            arms = item["arms"]
            shift = (index + current_seed) % len(arms)
            for current_arm in arms[shift:] + arms[:shift]:
                if arm and arm != current_arm:
                    continue
                if any((row["case"], row["seed"], row["arm"]) == (item["case"], current_seed, current_arm)
                       for row in rows):
                    continue
                print("START", item["case"], current_seed, current_arm, flush=True)
                row = run_one(root, item["case"], current_seed, current_arm)
                rows.append(row)
                framework.write_json(root / "matrix.json", rows)
                print("END", item["case"], current_seed, current_arm, row.get("process_exit_code"), flush=True)
                framework.backend_ready()


def validate_preregistration(payload: object) -> None:
    if not isinstance(payload, dict):
        raise ValueError("preregistration must be an object")
    if tuple(payload.get("arms", ())) != ARMS:
        raise ValueError("preregistration arms must be baseline,gap_directive")
    if payload.get("treatment") != GAP_DIRECTIVE:
        raise ValueError("candidate treatment does not match the frozen contract")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("worker", "check", "freeze", "run"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:5001")
    parser.add_argument("--case")
    parser.add_argument("--seed", type=int)
    parser.add_argument("--arm", choices=ARMS)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.action == "check":
        validate_preregistration(json.loads((root / "preregistration.json").read_text(encoding="utf-8")))
        print("OK", root)
        return
    if args.action == "freeze":
        freeze(root)
        print("FROZEN", root)
        return
    if args.action == "run":
        run_matrix(root, case=args.case, seed=args.seed, arm=args.arm)
        return
    if args.directory is None or args.case is None or args.seed is None or args.arm is None:
        raise SystemExit("worker requires --directory, --case, --seed and --arm")
    worker(root, args.directory.resolve(), args.base_url, args.case, args.seed, args.arm)


if __name__ == "__main__":
    main()
