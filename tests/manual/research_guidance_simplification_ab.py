"""A/B test a shorter semantic Research guidance block.

Only the model-facing semantic guidance is changed in child workers. Submission,
knowledge, citation, tool and publisher contracts remain the production ones.
"""

from __future__ import annotations

import argparse
import hashlib
import json
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

ARMS = ("baseline", "compact", "long_control", "production")
CASES = ("error_complex", "version_scope", "condition_scope", "known_url", "two_errors", "source_discovery")
SEEDS = (651, 652)


def compact_guidance(original: str) -> str:
    """Keep semantic safeguards; leave the submission/storage protocol intact."""
    marker = "Use state=answered"
    suffix = original[original.index(marker):]
    return (
        "Answer every requested point in order. For each point keep the same object, action, version and setting "
        "as the source. Preserve all conditions, exceptions, numbers and AND/OR alternatives. Keep source facts, "
        "supported deductions and missing evidence separate. A general statement, nearby operation or excerpt "
        "does not establish the requested relation or absence from the full source. Do not invent facts or gaps; "
        "retain uncertainty. Cite supported statements with [eN]. "
        + suffix
    )


@contextmanager
def treatment(arm: str, *, reference: str | None = None):
    from codey.research import answer_context, answer_contract

    if arm not in ARMS:
        raise ValueError(arm)
    if arm in {"baseline", "production"}:
        yield
        return
    guidance = reference if arm == "long_control" else compact_guidance(answer_contract.ANSWER_GUIDANCE)
    if guidance is None:
        raise ValueError("long_control requires its frozen original guidance")
    with ExitStack() as stack:
        stack.enter_context(patch.object(answer_contract, "ANSWER_GUIDANCE", guidance))
        stack.enter_context(patch.object(answer_context, "ANSWER_GUIDANCE", guidance))
        yield


def worker(root: Path, directory: Path, base_url: str, case: str, seed: int, arm: str):
    @contextmanager
    def selected(name):
        with treatment(name, reference=(root / "baseline-guidance.txt").read_text(encoding="utf8")):
            yield

    with patch.object(branch, "ablation", selected):
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
                                 genkey="guidance-simplification-" + uuid4().hex,
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


def prepare(root: Path, previous: Path, *, cases=CASES):
    import shutil

    root.mkdir(parents=False, exist_ok=False)
    for name in ("clock-context.txt", "material-channel.json", "seed-identities.json", "fixtures.json"):
        shutil.copy2(previous / name, root / name)
    for name in ("empty-knowledge", "warm-knowledge"):
        shutil.copytree(previous / name, root / name)
    framework.write_json(root / "limits.json", {"turns": 18, "requests": 12, "process_seconds": 900, "agent_seconds": 840})
    from codey.research.answer_contract import ANSWER_GUIDANCE

    (root / "baseline-guidance.txt").write_text(ANSWER_GUIDANCE, encoding="utf8")
    fixtures = framework.read_json(root / "fixtures.json")
    existing = {case["id"] for case in fixtures["cases"]}
    missing = set(cases) - existing
    if missing:
        raise ValueError(f"previous fixture is missing cases: {sorted(missing)}")
    framework.write_json(root / "preregistration.json", {
        "phase": "A/B", "seeds": list(SEEDS),
        "matrix": [{"case": case, "arms": ["baseline", "compact"]} for case in cases],
        "treatment": (
            "Answer every requested point in order. For each point keep the same object, action, version and setting "
            "as the source. Preserve all conditions, exceptions, numbers and AND/OR alternatives. Keep source facts, "
            "supported deductions and missing evidence separate. A general statement, nearby operation or excerpt "
            "does not establish the requested relation or absence from the full source. Do not invent facts or gaps; "
            "retain uncertainty. Cite supported statements with [eN]. <production submission/storage protocol>"
        ),
        "baseline": "Installed production guidance.",
        "gate": ["Grade body correctness and exact source scope separately from delivery.",
                 "Count all model requests, failed retries and input tokens.",
                 "Do not modify production code during the matrix."],
        "limitations": "Six fixed cases and two seeds per arm; exploratory evidence.",
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
    if (root / "baseline-guidance.txt").exists():
        names.append("baseline-guidance.txt")
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
    parser.add_argument("--cases", nargs="+", default=CASES)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.action == "prepare":
        prepare(root, args.previous.resolve(), cases=args.cases)
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
