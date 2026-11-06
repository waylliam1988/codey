"""③ live A/B for preserving object/version/limitation coverage on overflow."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

from tests.manual import research_window_coverage_candidate as coverage
from tests.manual import research_window_retention_ab as base
from tests.manual.research_experiment_identity import freeze_experiment, verify_experiment
from tests.manual.research_window_retention_cases import cases as all_cases

ARMS = ("baseline", "coverage")


def fixtures(phase):
    result = []
    for original in all_cases():
        if original["split"] != phase:
            continue
        case = json.loads(json.dumps(original))
        for document in case["documents"]:
            document["text"] += "\n" + ("Coverage pressure archive filler object version limitation exception. " * 500)
        result.append(case)
    return result


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def prepare(root, phase):
    root.mkdir(parents=True, exist_ok=False)
    fs = fixtures(phase)
    write(root / "fixtures.json", {"schema_version": 1, "cases": fs})
    write(root / "limits.json", {"turns": 18, "requests": 10, "process_seconds": 660, "agent_seconds": 600})
    write(root / "material-channel.json", {"mode": "read", "protocol": "plain"})
    clock = Path("tests/fixtures/research_local_clock.txt").read_text(encoding="utf-8")
    (root / "clock-context.txt").write_text(clock.replace("2026-11-01", "2026-11-06"), encoding="utf-8")
    write(root / "preregistration.json", {
        "experiment": "③", "phase": phase, "seeds": [1201] if phase == "dev" else [1301, 1302], "arms": ARMS,
        "work_view_cap": 3500, "provider_window": 32768,
        "treatment": "On overflow only, select whole observed windows by preserving coverage of object, version and limitation/exception facets from the original task; no summaries or prompt changes.",
        "primary": "strict complete task with all predeclared comparison facets and exact citations",
        "mechanism": "at least two paired runs must overflow and baseline must omit a facet while the candidate retains it",
        "gate": ["At least two effective heldout overflow pairs", "candidate strict improvement >=2 task-seed runs", "no new severe regression", "all-fit context identical", "default-budget confirmation required before promotion"],
        "scoring": "Source-based review against original task and actual read snapshots; writer self-verdict is not gold.",
        "gold_hash": hashlib.sha256(json.dumps(fs, sort_keys=True).encode()).hexdigest(),
    })
    write(root / "backend.json", {"model": base.get("/api/v1/model"), "version": base.get("/api/extra/version")})
    for folder in ("runs", "results", "logs", "wire"):
        (root / folder).mkdir()
    write(root / "frozen-inputs.json", {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.iterdir() if p.is_file()})
    freeze_experiment(root)


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def verify(root):
    verify_experiment(root)
    for name, digest in read(root / "frozen-inputs.json").items():
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != digest:
            raise RuntimeError("frozen input changed: " + name)


def worker(root, directory, base_url, case_id, seed, arm):
    verify(root)
    original_candidate = base.candidate
    base.candidate = coverage
    try:
        with patch.object(base, "ARMS", ARMS), patch.object(base, "cases", lambda: fixtures(read(root / "preregistration.json")["phase"])):
            base.worker(root, directory, base_url, case_id, seed, arm)
    finally:
        base.candidate = original_candidate


def run_one(root, case_id, seed, arm):
    verify(root)
    name = f"{case_id}-{seed}-{arm}"
    target = root / "results" / (name + ".json")
    if target.exists():
        return
    directory = root / "runs" / name
    if directory.exists():
        raise RuntimeError("unfinished run retained: " + name)
    limits = read(root / "limits.json")
    started = time.monotonic()
    out_path, err_path = root / "logs" / (name + ".stdout"), root / "logs" / (name + ".stderr")
    with out_path.open("w", encoding="utf-8") as out, err_path.open("w", encoding="utf-8") as err:
        process = subprocess.Popen([sys.executable, "-X", "utf8", "-m", __spec__.name, "worker", "--root", str(root), "--directory", str(directory), "--base-url", base.framework.UPSTREAM, "--case", case_id, "--seed", str(seed), "--arm", arm], stdout=out, stderr=err)
        try:
            process.communicate(timeout=limits["process_seconds"])
        except subprocess.TimeoutExpired:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
            process.communicate(timeout=15)
    row = {"case": case_id, "seed": seed, "arm": arm, "process_exit": process.returncode, "seconds": time.monotonic() - started}
    if (directory / "research-result.json").exists():
        row["result"] = read(directory / "research-result.json")
    write(target, row)
    verify(root)
    if process.returncode:
        raise RuntimeError("run failed: " + name)


def run(root):
    verify(root)
    spec = read(root / "preregistration.json")
    for i, case in enumerate(read(root / "fixtures.json")["cases"]):
        for seed in spec["seeds"]:
            arms = ARMS if (i + seed) % 2 else tuple(reversed(ARMS))
            for arm in arms:
                run_one(root, case["id"], seed, arm)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "run", "worker"))
    parser.add_argument("--phase", choices=("dev", "holdout"), default="dev")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--base-url")
    parser.add_argument("--case")
    parser.add_argument("--seed", type=int)
    parser.add_argument("--arm", choices=ARMS)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.action == "prepare":
        prepare(root, args.phase)
    elif args.action == "run":
        run(root)
    else:
        worker(root, args.directory, args.base_url, args.case, args.seed, args.arm)


if __name__ == "__main__":
    main()
