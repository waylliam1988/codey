"""Real-network A/B for HyperResearch-inspired acquisition guidance.

Both arms use the current production source discovery (including the adopted
2A browser-after-connector behavior).  The candidate changes only the
model-facing research instruction: search from several task-relevant angles,
open primary material, read the requested section, and fetch one targeted gap
when it could change the answer.  This module is experimental and does not
modify production defaults.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import socket
import time
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen

from tests.manual import research_live_web_local_ab as live

ARMS = ("baseline", "multi_angle_gap")
DEV_CASES = (
    "pubmed_hepatotoxicity",
    "arxiv_rag",
    "pubmed_myocarditis",
    "arxiv_long_context",
)

ACQUISITION_GUIDANCE = (
    "Research acquisition guidance: before answering, cover the distinct parts of the task with separate, "
    "task-relevant searches when useful: basic facts, the original or official source, comparison conditions, "
    "and limitations or counterexamples. Prefer the primary source for decisive claims. Open the source and read "
    "the section that supports the claim; a title, snippet, abstract, or search result is only a lead. Then check "
    "which requested fact, comparison side, condition, exception, or limitation remains unsupported. If a missing "
    "piece could change the answer, make one targeted search or read for that gap before submitting. Keep exact "
    "source identity, version, scope, and citation binding. Do not claim coverage merely because more results were "
    "found."
)


def question(case: str, arm: str) -> str:
    if case not in live.QUESTIONS:
        raise ValueError(case)
    if arm not in ARMS:
        raise ValueError(arm)
    base = live.QUESTIONS[case]
    return base if arm == "baseline" else base + "\n\n" + ACQUISITION_GUIDANCE


def prepare(root: Path, *, phase: str = "dev") -> None:
    if root.exists() and any(root.iterdir()):
        raise FileExistsError(f"experiment root is not empty: {root}")
    root.mkdir(parents=True, exist_ok=True)
    spec = {
        "experiment": "hyperresearch-acquisition-v1",
        "phase": phase,
        "arms": list(ARMS),
        "cases": list(DEV_CASES),
        "seeds": [2101] if phase == "dev" else [2201, 2202],
        "quality_first": True,
        "cost_diagnostics_only": True,
        "treatment": ACQUISITION_GUIDANCE,
        "production_unchanged_until_quality_gate": True,
        "gate": [
            "Compare task-level original-source discovery, section/full-text reads, coverage of requested facets, supported answer completeness and citation correctness.",
            "Keep all failures and distinguish mechanism-not-triggered from candidate regression.",
            "Do not promote from development; freeze a new-task double-seed matrix only after mechanism and quality evidence.",
        ],
    }
    (root / "preregistration.json").write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def freeze(root: Path, base_url: str) -> None:
    spec_path = root / "preregistration.json"
    if not spec_path.exists():
        raise FileNotFoundError(spec_path)
    with urlopen(base_url + "/v1/models", timeout=5) as response:
        model = json.load(response)
    (root / "backend-model.json").write_text(json.dumps(model, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    digest = hashlib.sha256(spec_path.read_bytes()).hexdigest()
    (root / "contract-hash.json").write_text(json.dumps({"sha256": digest}, indent=2) + "\n", encoding="utf-8")


def run_matrix(root: Path, base_url: str) -> None:
    spec = json.loads((root / "preregistration.json").read_text(encoding="utf-8"))
    rows_path = root / "matrix.json"
    rows = json.loads(rows_path.read_text(encoding="utf-8")) if rows_path.exists() else []
    model = json.loads((root / "backend-model.json").read_text(encoding="utf-8"))["data"][0]["id"]
    for index, case in enumerate(spec["cases"]):
        for seed in spec["seeds"]:
            arms = tuple(spec["arms"])
            ordered = arms[index % len(arms):] + arms[:index % len(arms)]
            for arm in ordered:
                key = (case, seed, arm)
                if any((row["case"], row["seed"], row["arm"]) == key for row in rows):
                    continue
                print("START", *key, flush=True)
                started = time.monotonic()
                try:
                    run(root, case, arm, seed, base_url, model)
                    row = {"case": case, "seed": seed, "arm": arm, "status": "completed"}
                except Exception as error:  # retain task failures and continue
                    row = {"case": case, "seed": seed, "arm": arm, "status": "failed", "error": repr(error)}
                row["elapsed_seconds"] = round(time.monotonic() - started, 2)
                rows.append(row)
                rows_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                print("END", *key, row["status"], flush=True)


def run(root: Path, case: str, arm: str, seed: int, base_url: str, model: str) -> None:
    with urlopen(base_url + "/api/extra/perf", timeout=10) as response:
        perf = json.load(response)
    if perf.get("idle") != 1 or perf.get("queue") != 0:
        raise RuntimeError(f"Backend is busy: {perf}")
    directory = root / f"{case}-{seed}-{arm}"
    started = time.monotonic()
    # Reuse the validated 2A live harness: it sets output budget, disables
    # backend reasoning, compiles schemas once, and installs production 2A
    # source discovery.  Only the user-facing question is patched here.
    scratch = root / ("b" if arm == "baseline" else "m")
    scratch.mkdir(parents=True, exist_ok=True)
    original = live.QUESTIONS[case]
    live.QUESTIONS[case] = question(case, arm)
    try:
        with patch.object(live, "OUTPUT_TOKENS", 16384):
            live.run(scratch, case, "browser_after_connector", seed, base_url, model)
    finally:
        live.QUESTIONS[case] = original
    produced = scratch / f"{case}-{seed}-browser_after_connector"
    if not produced.exists():
        raise RuntimeError(f"live harness did not produce {produced}")
    shutil.move(str(produced), str(directory))
    (directory / "acquisition-observation.json").write_text(
        json.dumps({"case": case, "arm": arm, "seed": seed,
                    "elapsed_seconds": round(time.monotonic() - started, 2),
                    "guidance": "baseline" if arm == "baseline" else ACQUISITION_GUIDANCE},
                   ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "freeze", "run", "worker"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--case", choices=DEV_CASES)
    parser.add_argument("--arm", choices=ARMS)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--base-url", default="http://127.0.0.1:5001")
    args = parser.parse_args()
    root = args.root.resolve()
    if args.action == "prepare":
        prepare(root)
        return
    if args.action == "freeze":
        freeze(root, args.base_url)
        return
    if args.action == "run":
        run_matrix(root, args.base_url)
        return
    if args.case is None or args.arm is None or args.seed is None:
        raise SystemExit("worker requires --case, --arm and --seed")
    with urlopen(args.base_url + "/v1/models", timeout=5) as response:
        model = json.load(response)["data"][0]["id"]
    with socket.socket() as lock:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            lock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        lock.bind(("127.0.0.1", 15002))
        run(root, args.case, args.arm, args.seed, args.base_url, model)
    print(json.dumps({"case": args.case, "arm": args.arm, "seed": args.seed}, ensure_ascii=False))


if __name__ == "__main__":
    main()
