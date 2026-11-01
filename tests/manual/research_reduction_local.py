"""Run arbitrary frozen material cases through Codey's ordinary research entry.

No prefilled plan/read, answer repair, alternate loop or model-based grading.
The transparent proxy and sampling/budget match the comparison harness.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen
from uuid import uuid4

from tests.manual.research_experiment_identity import freeze_experiment, verify_experiment
from tests.manual.research_framework_comparison import (
    UPSTREAM,
    InferenceProxy,
    backend_ready,
    comparison_config,
    write_json,
)


def task_for_case(case):
    return (case["question"] + "\n用中文回答。仅根据本轮固定材料，不使用外部知识。"
            "先用可用的来源工具读取原文，材料不足时明确保留缺口；保留条件、否定和例外，"
            "不补充旁支机制。以下 URL 在本轮读取接口中映射到固定材料：\n"
            + "\n".join(doc["url"] for doc in case["documents"]))


def load_cases(path):
    rows = json.loads(Path(path).read_text(encoding="utf-8"))["cases"]
    if (not rows or len({row["id"] for row in rows}) != len(rows)
            or any(not re.fullmatch(r"[a-z0-9_-]+", row["id"]) or not row["question"].strip()
                   or not row["documents"] or any(not doc["text"].strip() for doc in row["documents"]) for row in rows)):
        raise ValueError("fixtures require unique case ids, nonempty questions and frozen documents")
    return {row["id"]: row for row in rows}


def worker(directory, case, base_url, seed):
    verify_experiment(directory.parent.parent)
    from codey.app import headless_runner
    from codey.providers import local_config
    from tests.manual import persistent_research_local as probe
    from tests.manual.research_evidence_local import DEFAULT_CLOCK_FILE, fixture_clock

    directory.mkdir(parents=True, exist_ok=False)
    documents = [{key: doc[key] for key in ("url", "title", "text")} for doc in case["documents"]]
    for doc in documents:
        doc["sha256"] = hashlib.sha256(doc["text"].encode()).hexdigest()
    write_json(directory / "sources.json", documents)
    with ExitStack() as stack:
        save_config = local_config.save_local_config
        stack.enter_context(patch.object(local_config, "save_local_config", lambda config: save_config(comparison_config(config))))
        stack.enter_context(patch("codey.providers.request_context.clock_context", return_value=fixture_clock(DEFAULT_CLOCK_FILE)))
        request_type = headless_runner.HeadlessRequest
        stack.enter_context(patch.object(headless_runner, "HeadlessRequest",
            lambda **kwargs: request_type(**{**kwargs, "strict_research": True})))
        with urlopen(UPSTREAM + "/v1/models", timeout=5) as response:
            model = json.load(response)["data"][0]["id"]
        probe.stage(directory, "research", base_url, model, native=True, live_web=False, question=task_for_case(case),
                    max_turns=12, seed=seed, provider_timeout=150, constrained_text=False)


def run_case(root, case, seed):
    verify_experiment(root)
    ready = backend_ready()
    name = f"{case}-{seed}"
    directory = root / "runs" / name
    started = time.monotonic()
    with InferenceProxy(root / "wire" / f"{name}.jsonl", seed, genkey="reduction-" + uuid4().hex, identity_root=root) as proxy, \
         (root / "logs" / f"{name}.stdout").open("w", encoding="utf-8") as out, \
         (root / "logs" / f"{name}.stderr").open("w", encoding="utf-8") as err:
        process = subprocess.Popen([sys.executable, "-X", "utf8", "-m", __spec__.name, "worker",
            "--directory", str(directory), "--fixtures", str(root / "fixtures.json"), "--case", case,
            "--seed", str(seed), "--base-url", proxy], stdout=out, stderr=err)
        timed_out = False
        try:
            process.communicate(timeout=360)
        except subprocess.TimeoutExpired:
            timed_out = True
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
            process.communicate(timeout=15)
    result = {"case": case, "seed": seed, "process_exit_code": process.returncode, "runner_timeout": timed_out,
              "backend_before": ready, "elapsed_seconds": round(time.monotonic() - started, 2),
              "semantic_grade": "pending_manual_review"}
    path = directory / "research-result.json"
    if path.exists():
        result["result"] = json.loads(path.read_text(encoding="utf-8"))
    write_json(directory / "experiment-result.json", result)
    verify_experiment(root)
    return result


def main():
    from codey.operations.provider_receipts import runtime_implementation_id

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["run", "worker"])
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--case", action="append")
    parser.add_argument("--seed", action="append", type=int)
    parser.add_argument("--base-url")
    args = parser.parse_args()
    cases = load_cases(args.fixtures)
    chosen = args.case or list(cases)
    seeds = args.seed or [74, 75, 76]
    if not set(chosen) <= cases.keys():
        parser.error("unknown frozen case")
    root = args.directory.resolve()
    if args.action == "worker":
        worker(root, cases[chosen[0]], args.base_url, seeds[0])
        return
    root.mkdir(parents=True, exist_ok=False)
    (root / "logs").mkdir()
    (root / "runs").mkdir()
    freeze_experiment(root)
    source = args.fixtures.read_bytes()
    (root / "fixtures.json").write_bytes(source)
    write_json(root / "implementation.json", {"commit": subprocess.check_output(["git", "rev-parse", "HEAD"],
        encoding="utf-8").strip(), "runtime_id": runtime_implementation_id(), "fixtures_sha256": hashlib.sha256(source).hexdigest(),
        "diff": subprocess.check_output(["git", "diff", "HEAD", "--", "codey"], encoding="utf-8"), "command": sys.argv})
    results = []
    for case in chosen:
        for seed in seeds:
            print("START", case, seed, flush=True)
            result = run_case(root, case, seed)
            results.append(result)
            write_json(root / "matrix.json", results)
            print("END", case, seed, result.get("result", {}).get("stop_reason"), flush=True)


if __name__ == "__main__":
    main()
