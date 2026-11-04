"""A/B for version-faithful synthesis and direct reading of known sources.

This harness changes only model-facing experimental guidance in child workers.
The installed production code, citation checks and publisher remain untouched.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen
from uuid import uuid4

from tests.manual import research_branch_review as branch
from tests.manual import research_framework_comparison as framework
from tests.manual.research_experiment_identity import freeze_experiment

ARMS = ("baseline", "faithful", "direct_source", "combined")
CASES = ("error_complex", "version_scope", "condition_scope", "known_url", "two_errors", "source_discovery")
SEEDS = (641, 642)

FAITHFUL_GUIDANCE = (
    "Keep each version or subject tied to its own original statements. Do not transfer a condition between "
    "versions or subjects. Keep explicit rules close to their original wording, preserving scope, numbers, "
    "exceptions and AND/OR alternatives. If a requested relationship is not stated, say so rather than "
    "inferring independence, necessity or sufficiency."
)

DIRECT_SOURCE_GUIDANCE = (
    "Read a provided, permitted source URL directly instead of searching for the same URL. Search when the task "
    "requires source discovery, no usable source is identified, or direct reading fails. A failed knowledge ID "
    "lookup does not show that a provided web URL failed."
)


def _guidance(arm: str) -> str:
    parts = []
    if arm in {"faithful", "combined"}:
        parts.append(FAITHFUL_GUIDANCE)
    if arm in {"direct_source", "combined"}:
        parts.append(DIRECT_SOURCE_GUIDANCE)
    return "\n".join(parts)


@contextmanager
def treatment(arm: str):
    from codey.research import answer_context, answer_contract

    if arm not in ARMS:
        raise ValueError(arm)
    extra = _guidance(arm)
    if not extra:
        yield
        return
    with ExitStack() as stack:
        guidance = answer_contract.ANSWER_GUIDANCE + "\n" + extra
        stack.enter_context(patch.object(answer_contract, "ANSWER_GUIDANCE", guidance))
        stack.enter_context(patch.object(answer_context, "ANSWER_GUIDANCE", guidance))
        yield


def worker(root: Path, directory: Path, base_url: str, case: str, seed: int, arm: str):
    @contextmanager
    def apply(name: str):
        with treatment(name):
            yield

    with patch.object(branch, "ablation", apply):
        branch.worker(root, directory, base_url, case, seed, arm)


def run_one(root: Path, case: str, seed: int, arm: str):
    branch.verify(root)
    ready = framework.backend_ready()
    name = f"{case}-{seed}-{arm}"
    directory = root / "runs" / name
    limits = framework.experiment_limits(root)
    started = time.monotonic()
    with (
        patch.object(framework, "verify_experiment", branch.verify),
        framework.InferenceProxy(root / "wire" / f"{name}.jsonl", seed,
                                 genkey="fidelity-source-" + uuid4().hex,
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
    row = {"case": case, "seed": seed, "arm": arm, "process_exit_code": process.returncode,
           "runner_timeout": timed_out, "elapsed_seconds": round(time.monotonic() - started, 2),
           "backend_before": ready, "semantic_grade": "pending_independent_review"}
    result = directory / "research-result.json"
    if result.exists():
        row["result"] = framework.read_json(result)
    framework.write_json(root / "results" / f"{name}.json", row)
    branch.verify(root)
    return row


def prepare(root: Path, previous: Path):
    root.mkdir(parents=False, exist_ok=False)
    for name in ("clock-context.txt", "material-channel.json", "seed-identities.json", "fixtures.json"):
        shutil.copy2(previous / name, root / name)
    for name in ("empty-knowledge", "warm-knowledge"):
        shutil.copytree(previous / name, root / name)
    framework.write_json(root / "limits.json", {"turns": 18, "requests": 12, "process_seconds": 900, "agent_seconds": 840})
    fixtures = framework.read_json(root / "fixtures.json")
    existing = {case["id"]: case for case in fixtures["cases"]}
    known = {**existing["accumulate"], "id": "known_url"}
    discovery = {**known, "id": "source_discovery", "warm": True, "knowledge_seed": "empty-knowledge",
        "question": "请先搜索找到 SQLite WAL 只读数据库条件的官方材料，再读取原文，用中文回答支持版本和完整替代条件，保留 OR。不要使用材料外知识，不需要保存新 claim。"}
    fixtures["cases"] = [existing["error_complex"], {
        "id": "version_scope",
        "question": "用中文分别核查 V7、V8 的 local/remote 额度、license 前提和读取支持。保留数字、每个条件的适用对象及原文未说明的部分；不要将不同版本的条件合并。",
        "documents": [
            {"url": "https://example.org/fidelity/vela-v7", "title": "SYNTHETIC Vela V7 policy",
             "text": "## V7\nV7 local requires a VALID license and allows 9 workers. V7 remote allows 3 workers and does not inherit the local allowance. V7 does not support read-only opening. Its receipt tag is CEDAR-2847.\n"},
            {"url": "https://example.org/fidelity/vela-v8", "title": "SYNTHETIC Vela V8 policy",
             "text": "## V8\nV8 local allows 13 workers with a VALID license and 5 without one. V8 remote allows 6 workers independently of license. Its receipt tag is MAPLE-6193.\n"},
        ],
    }, {
        "id": "condition_scope",
        "question": "用中文分别说明 R4、R5 的导出许可条件、例外、缓存的作用和保留天数；准确保留且/或。再说明材料是否给出只读支持或真实耗时，未说明的关系如实保留。",
        "documents": [
            {"url": "https://example.org/fidelity/export-r4", "title": "SYNTHETIC R4 export rules",
             "text": "## R4\nR4 permits export when BOTH a valid audit AND a valid signature are present, OR when the emergency flag is set. An existing cache alone is insufficient. R4 retention is exactly 21 days. These are permission rules, not speed measurements.\n"},
            {"url": "https://example.org/fidelity/export-r5", "title": "SYNTHETIC R5 export rules",
             "text": "## R5\nR5 permits export with a valid audit OR a valid signature. The emergency flag alone is insufficient for R5. R5 retention is exactly 28 days. These rules do not specify read-only support or elapsed time.\n"},
        ],
    }, known, existing["two_errors"], discovery]
    framework.write_json(root / "fixtures.json", fixtures)
    framework.write_json(root / "preregistration.json", {
        "phase": "A/B",
        "seeds": list(SEEDS),
        "matrix": [{"case": case, "arms": list(ARMS)} for case in CASES],
        "treatments": {
            "faithful": FAITHFUL_GUIDANCE,
            "direct_source": DIRECT_SOURCE_GUIDANCE,
            "combined": FAITHFUL_GUIDANCE + "\n" + DIRECT_SOURCE_GUIDANCE,
        },
        "baseline": "Installed production guidance, with no experimental patch.",
        "presentation": "Append generic guidance once in the work view; no tool schema, source text or validator changes.",
        "gate": [
            "Grade body correctness and exact source scope separately from delivery.",
            "Count all model requests, failed retries and input tokens.",
            "Check whether known URLs are opened directly instead of searched first.",
            "Do not modify production code during or after the matrix; use a new frozen root for changes.",
        ],
        "limitations": "Six fixed cases and two fresh seeds per arm. Source discovery uses the common fixture's warm task-text flag with an explicitly empty knowledge seed only to avoid injecting the answer URL. All other stage/reading/publishing behavior is unchanged. No answer-specific example in generic treatment. Exploratory evidence.",
    })


def freeze(root: Path):
    for name in ("runs", "wire", "logs", "results"):
        (root / name).mkdir(exist_ok=True)
    framework.backend_ready()
    with urlopen(framework.UPSTREAM + "/v1/models", timeout=5) as response:
        framework.write_json(root / "backend-model.json", json.load(response))
    framework.write_json(root / "production-sha256.json", {
        path.as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(Path("codey").rglob("*.py"))})
    names = ["preregistration.json", "backend-model.json", "seed-identities.json", "clock-context.txt",
             "limits.json", "fixtures.json", "material-channel.json", "production-sha256.json"]
    if (root / "review-rubric.json").exists():
        names.append("review-rubric.json")
    for folder in ("warm-knowledge", "empty-knowledge"):
        names.extend(path.relative_to(root).as_posix() for path in sorted((root / folder).rglob("*")) if path.is_file())
    framework.write_json(root / "frozen-inputs.json", {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in names})
    freeze_experiment(root)
    branch.verify(root)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "freeze", "run", "worker"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--previous", type=Path)
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--base-url")
    parser.add_argument("--case")
    parser.add_argument("--seed", type=int)
    parser.add_argument("--arm", choices=ARMS)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.action == "prepare":
        prepare(root, args.previous.resolve())
        return
    if args.action == "freeze":
        freeze(root)
        print("FROZEN", root, flush=True)
        return
    if args.action == "worker":
        worker(root, args.directory, args.base_url, args.case, args.seed, args.arm)
        return
    branch.verify(root)
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
                if any((row["case"], row["seed"], row["arm"]) == (item["case"], seed, arm) for row in rows):
                    continue
                print("START", item["case"], seed, arm, flush=True)
                row = run_one(root, item["case"], seed, arm)
                rows.append(row)
                framework.write_json(matrix, rows)
                print("END", item["case"], seed, arm, row.get("result"), flush=True)
                framework.backend_ready()


if __name__ == "__main__":
    main()
