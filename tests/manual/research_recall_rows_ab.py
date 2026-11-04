"""Live A/B for strict Research recall rows: compact versus complete rows."""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from tests.manual import research_branch_review as branch
from tests.manual import research_framework_comparison as framework

ARMS = ("baseline", "full_rows")


@contextmanager
def treatment(arm: str):
    if arm not in ARMS:
        raise ValueError(arm)
    from codey.operations import knowledge_context

    with ExitStack() as stack:
        if arm == "full_rows":
            original = knowledge_context.render_recall

            def recalled(*args, **kwargs):
                if kwargs.get("compact"):
                    kwargs = {**kwargs, "compact": False}
                return original(*args, **kwargs)

            stack.enter_context(patch.object(knowledge_context, "render_recall", recalled))
        yield


def worker(root: Path, directory: Path, base_url: str, case: str, seed: int, arm: str):
    with patch.object(branch, "ablation", treatment):
        branch.worker(root, directory, base_url, case, seed, arm)


def run_one(root: Path, case: str, seed: int, arm: str) -> dict:
    branch.verify(root)
    ready = framework.backend_ready()
    name = f"{case}-{seed}-{arm}"
    directory = root / "runs" / name
    limits = framework.experiment_limits(root)
    started = time.monotonic()
    with (
        patch.object(framework, "verify_experiment", branch.verify),
        framework.InferenceProxy(root / "wire" / f"{name}.jsonl", seed,
                                 genkey="recall-rows-" + uuid4().hex,
                                 request_limit=limits["requests"], identity_root=root) as proxy,
        (root / "logs" / f"{name}.stdout").open("w", encoding="utf-8") as out,
        (root / "logs" / f"{name}.stderr").open("w", encoding="utf-8") as err,
    ):
        process = subprocess.Popen([
            sys.executable, "-X", "utf8", "-m", __spec__.name, "worker",
            "--root", str(root), "--directory", str(directory), "--base-url", proxy,
            "--case", case, "--seed", str(seed), "--arm", arm,
        ], stdout=out, stderr=err)
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
    result = directory / "research-result.json"
    if result.exists():
        row["result"] = framework.read_json(result)
    framework.write_json(root / "results" / f"{name}.json", row)
    branch.verify(root)
    return row


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("run", "worker"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--base-url")
    parser.add_argument("--case")
    parser.add_argument("--seed", type=int)
    parser.add_argument("--arm", choices=ARMS)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.action == "worker":
        worker(root, args.directory, args.base_url, args.case, args.seed, args.arm)
        return
    rows = []
    for case in ("history", "history_error", "inference", "conflict"):
        for seed in (81, 82):
            for arm in ARMS:
                print("START", case, seed, arm, flush=True)
                row = run_one(root, case, seed, arm)
                rows.append(row)
                framework.write_json(root / "recall-rows-results.json", rows)
                print("END", case, seed, arm,
                      (row.get("result") or {}).get("stop_reason", "runner_failure"), flush=True)


if __name__ == "__main__":
    main()
