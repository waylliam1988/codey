"""Small real-model A/B for HyperResearch-inspired evidence presentation.

This reuses the frozen Research harness but changes only model-facing
presentation in isolated workers. It compares one baseline with one focused
candidate for each remaining problem: clause-to-quote checking, exact linked
source navigation, and long-document read navigation. No production module is
modified by the experiment.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from tests.manual import research_framework_comparison as framework
from tests.manual import research_state_fidelity_ab as state

BASE_TREATMENT = state.treatment


CLAUSE_QUOTE_GUIDANCE = (
    "For each important conclusion, split the conclusion into separate clauses and check each clause against the "
    "cited original passage. Keep a short contiguous original quote beside the clause when it carries a condition, "
    "number, comparator, version or exception. A quote must support the clause as written; matching topic words are "
    "not enough. If a clause has no direct supporting quote, mark it unsupported or uncertain instead of adding a "
    "necessary, sufficient, AND/OR or causal relationship. Preserve the source's implication direction."
)


@contextmanager
def treatment(arm: str):
    if arm == "clause_quotes":
        from codey.research import answer_context, answer_contract

        with ExitStack() as stack:
            original = answer_contract.ANSWER_GUIDANCE
            guidance = original + " " + CLAUSE_QUOTE_GUIDANCE
            stack.enter_context(patch.object(answer_contract, "ANSWER_GUIDANCE", guidance))
            stack.enter_context(patch.object(answer_context, "ANSWER_GUIDANCE", guidance))
            yield
        return
    if arm in {"source_links", "read_navigation"}:
        from codey.research import answer_context

        original = answer_context._render_context
        selected = "reference_actions" if arm == "source_links" else "reading_actions"
        with BASE_TREATMENT(selected):
            yield
        return
    if arm == "baseline":
        yield
        return
    raise ValueError(arm)


def worker(root: Path, directory: Path, base_url: str, case: str, seed: int, arm: str):
    with patch.object(state, "treatment", treatment):
        state.worker(root, directory, base_url, case, seed, arm)


def prepare(root: Path) -> None:
    state.prepare(root)
    spec = framework.read_json(root / "preregistration.json")
    cases = {
        "synthetic_conditions": "clause_quotes",
        "inference": "source_links",
        "overflow": "read_navigation",
    }
    spec["phase"] = "independent HyperResearch-inspired A/B; no production edits"
    spec["matrix"] = [{"case": case, "arms": ["baseline", arm]} for case, arm in cases.items()]
    spec["treatments"].update({
        "clause_quotes": CLAUSE_QUOTE_GUIDANCE,
        "source_links": "Show exact unread pinned source/evidence read actions; navigation is not evidence.",
        "read_navigation": "Show actual section read/partial/unread status and exact next offsets.",
    })
    spec["limitations"] = "Three frozen cases and two seeds per arm; exploratory evidence only."
    framework.write_json(root / "preregistration.json", spec)


def run_one(root: Path, case: str, seed: int, arm: str) -> dict:
    state.branch.verify(root)
    ready = framework.backend_ready()
    name = f"{case}-{seed}-{arm}"
    directory = root / "runs" / name
    limits = framework.experiment_limits(root)
    started = time.monotonic()
    with patch.object(framework, "verify_experiment", state.branch.verify), framework.InferenceProxy(
            root / "wire" / f"{name}.jsonl", seed, genkey="hyperresearch-inspiration-" + uuid4().hex,
            request_limit=limits["requests"], identity_root=root) as proxy, \
            (root / "logs" / f"{name}.stdout").open("w", encoding="utf-8") as out, \
            (root / "logs" / f"{name}.stderr").open("w", encoding="utf-8") as err:
        process = subprocess.Popen([sys.executable, "-X", "utf8", "-m", __spec__.name, "worker",
            "--root", str(root), "--directory", str(directory), "--base-url", proxy,
            "--case", case, "--seed", str(seed), "--arm", arm], stdout=out, stderr=err)
        timed_out = False
        try:
            process.communicate(timeout=limits["process_seconds"])
        except subprocess.TimeoutExpired:
            timed_out = True
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
            process.communicate(timeout=15)
    row = {"arm": arm, "case": case, "seed": seed, "process_exit_code": process.returncode,
           "runner_timeout": timed_out, "elapsed_seconds": round(time.monotonic() - started, 2),
           "backend_before": ready, "semantic_grade": "pending_independent_review"}
    if (directory / "research-result.json").exists():
        row["result"] = framework.read_json(directory / "research-result.json")
    framework.write_json(root / "results" / f"{name}.json", row)
    state.branch.verify(root)
    return row


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "freeze", "run", "worker"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--base-url")
    parser.add_argument("--case")
    parser.add_argument("--seed", type=int)
    parser.add_argument("--arm")
    args = parser.parse_args()
    root = args.root.resolve()
    if args.action == "prepare":
        prepare(root)
    elif args.action == "freeze":
        state.freeze(root)
        print("FROZEN", root, flush=True)
    elif args.action == "worker":
        worker(root, args.directory, args.base_url, args.case, args.seed, args.arm)
    else:
        state.branch.verify(root)
        spec = framework.read_json(root / "preregistration.json")
        matrix = root / "matrix.json"
        rows = framework.read_json(matrix) if matrix.exists() else []
        for index, item in enumerate(spec["matrix"]):
            if args.case and args.case != item["case"]:
                continue
            for seed in spec["seeds"]:
                if args.seed is not None and args.seed != seed:
                    continue
                arms = item["arms"]
                shift = (index + seed) % len(arms)
                for arm in arms[shift:] + arms[:shift]:
                    if args.arm and args.arm != arm:
                        continue
                    if any((row["case"], row["seed"], row["arm"]) == (item["case"], seed, arm)
                           for row in rows):
                        continue
                    print("START", item["case"], seed, arm, flush=True)
                    row = run_one(root, item["case"], seed, arm)
                    rows.append(row)
                    framework.write_json(matrix, rows)
                    print("END", item["case"], seed, arm, flush=True)
                    framework.backend_ready()


if __name__ == "__main__":
    main()
