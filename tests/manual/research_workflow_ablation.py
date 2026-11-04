"""Frozen workflow ablations through the original headless research runtime.

Manual only: patches live solely in each experiment worker, never in production.
The reference proxy preserves wire requests/responses and bounds inference.
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

from tests.manual import research_framework_comparison as framework
from tests.manual.research_experiment_identity import freeze_experiment, verify_experiment

ARMS = ("baseline", "dedup_rules", "no_auto_recall")


@contextmanager
def ablation(arm):
    if arm not in ARMS:
        raise ValueError(f"unknown ablation: {arm}")
    from codey.operations import kernel_prompt, knowledge_context
    from codey.research import answer_contract

    with ExitStack() as stack:
        if arm == "dedup_rules":
            original = kernel_prompt.kernel_prompt_for_session

            def deduplicate(session, **kwargs):
                if kwargs.get("whole_submission"):
                    guidance = answer_contract.ANSWER_GUIDANCE
                    # The original KernelContext.freeze adds answer_context as
                    # request-scoped observations after this prompt is built.
                    kwargs["task_guidance"] = kwargs.get("task_guidance", "").replace(guidance, "")
                return original(session, **kwargs)

            stack.enter_context(patch.object(kernel_prompt, "kernel_prompt_for_session", deduplicate))
        elif arm == "no_auto_recall":
            original_background = knowledge_context.knowledge_background

            def explicit_recall_only(tools, session, question):
                if answer_contract.whole_research(session):
                    return ""
                return original_background(tools, session, question)

            stack.enter_context(patch.object(knowledge_context, "knowledge_background", explicit_recall_only))
        yield


def verify(root):
    verify_experiment(root)
    expected = framework.read_json(root / "preregistered-sha256.json")
    for name, digest in expected.items():
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != digest:
            raise RuntimeError(f"frozen experimental specification changed: {name}")


def seed_history(directory, case):
    """Explicit warm fixture, not model output or a scored cold-start success."""
    from codey.knowledge.model import KnowledgeMutation as M
    from codey.knowledge.model import KnowledgeObject as K
    from codey.knowledge.model import KnowledgeReference as R
    from codey.knowledge.store import KnowledgeStore

    store = KnowledgeStore(directory / "knowledge")
    try:
        doc = case["documents"][0]
        common = {"scope": "personal", "scope_id": store.owner_id}
        store.repository.commit("ablation-warm-fixture", [
            M(K("historical-wal-source", "source", doc["text"], "source", title=doc["title"],
                provenance={"url": doc["url"]}, **common)),
            M(K("historical-wal-evidence", "evidence", doc["text"], "observed_result",
                title="SQLite WAL read-only original evidence",
                references=(R("historical-wal-source", 1, "from_source"),), **common)),
            M(K("historical-wal-claim", "claim",
                "SQLite WAL 只读条件的历史候选记录；具体版本、替代条件应核查关联原始证据。",
                "model_extraction", title="SQLite WAL 只读历史记录",
                aliases=("SQLite", "WAL", "只读"),
                references=(R("historical-wal-evidence", 1, "supported_by"),), **common)),
        ])
        framework.write_json(directory / "warm-seed.json", [
            store.repository.read(identity, 1).to_dict()
            for identity in ("historical-wal-source", "historical-wal-evidence", "historical-wal-claim")
        ])
    finally:
        store.close()


def worker(root, directory, base_url, case_id, seed, arm):
    verify(root)
    from tests.manual import persistent_research_local as probe

    case = framework.custom_cases(root)[case_id]
    original_stage = probe.stage
    original_task = framework.task_text

    def stage_with_history(directory, *args, **kwargs):
        if case.get("warm"):
            seed_history(directory, case)
        return original_stage(directory, *args, **kwargs)

    def task_text(root, case_id):
        return case["question"] if case.get("warm") else original_task(root, case_id)

    with ablation(arm), patch.object(probe, "stage", stage_with_history), \
            patch.object(framework, "task_text", task_text):
        framework.codey_worker(root, directory, base_url, case_id, seed)
    verify(root)


def run_one(root, case, seed, arm):
    verify(root)
    ready = framework.backend_ready()
    name = f"{case}-{seed}-{arm}"
    directory = root / "runs" / name
    started = time.monotonic()
    limits = framework.experiment_limits(root)
    original_sampling = framework.sampling

    def checked_sampling(payload, seed):
        if arm == "dedup_rules":
            from codey.research.answer_contract import ANSWER_GUIDANCE

            if not any(ANSWER_GUIDANCE in str(message.get("content", "")) for message in payload["messages"]):
                raise AssertionError("dedup candidate lost authoritative research rules before inference")
        return original_sampling(payload, seed)

    with patch.object(framework, "verify_experiment", verify), \
            patch.object(framework, "sampling", checked_sampling), \
            framework.InferenceProxy(root / "wire" / f"{name}.jsonl", seed,
            genkey="workflow-ablation-" + uuid4().hex, request_limit=limits["requests"],
            identity_root=root) as proxy, \
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
    result = {"case": case, "seed": seed, "arm": arm, "process_exit_code": process.returncode,
              "runner_timeout": timed_out, "elapsed_seconds": round(time.monotonic() - started, 2),
              "backend_before": ready, "semantic_grade": "pending_independent_review"}
    path = directory / "research-result.json"
    if path.exists():
        result["result"] = framework.read_json(path)
    framework.write_json(root / "results" / f"{name}.json", result)
    verify(root)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("freeze", "run", "worker"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--base-url")
    parser.add_argument("--case")
    parser.add_argument("--seed", type=int, default=74)
    parser.add_argument("--arm", choices=ARMS)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.action == "worker":
        worker(root, args.directory, args.base_url, args.case, args.seed, args.arm)
        return
    if args.action == "freeze":
        for name in ("runs", "logs", "wire", "results"):
            (root / name).mkdir(exist_ok=True)
        framework.backend_ready()
        with urlopen(framework.UPSTREAM + "/v1/models", timeout=5) as response:
            framework.write_json(root / "backend-model.json", json.load(response))
        framework.write_json(root / "preregistered-sha256.json", {
            name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in ("preregistration.json", "backend-model.json")})
        freeze_experiment(root)
        verify(root)
        print("Frozen", root, flush=True)
        return
    verify(root)
    spec = framework.read_json(root / "preregistration.json")
    matrix_path = root / "matrix.json"
    rows = framework.read_json(matrix_path) if matrix_path.exists() else []
    for index, case in enumerate(spec["cases"]):
        for seed in spec["seeds"]:
            # Rotate order deterministically to spread warm-cache/order effects.
            shift = (index + seed) % len(ARMS)
            order = ARMS[shift:] + ARMS[:shift]
            for arm in order:
                if any((r["case"], r["seed"], r["arm"]) == (case, seed, arm) for r in rows):
                    continue
                print("START", case, seed, arm, flush=True)
                row = run_one(root, case, seed, arm)
                rows.append(row)
                framework.write_json(matrix_path, rows)
                print("END", case, seed, arm, row.get("result"), flush=True)
                # Do not hammer a wedged backend or classify its faults as model errors.
                framework.backend_ready()


if __name__ == "__main__":
    main()
