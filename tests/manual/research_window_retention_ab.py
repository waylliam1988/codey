"""End-to-end Research A/B; the sole arm difference is overflow window choice.

python -m tests.manual.research_window_retention_ab prepare --phase dev
python -m tests.manual.research_window_retention_ab run --phase dev
Repeat prepare/run with --phase holdout. No scripted model tool decisions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from codey.research import answer_context as context
from tests.manual import research_framework_comparison as framework
from tests.manual import research_window_retention_candidate as candidate
from tests.manual.research_experiment_identity import freeze_experiment, verify_experiment
from tests.manual.research_semantic_review_ab import idle_backend
from tests.manual.research_window_retention_cases import cases

ROOT = Path(".e2e-artifacts/research-window-retention-v2-20261106")
ARMS = ("baseline", "relevance")
BASE_VIEW = context.answer_context
BASE_LIMITS = context.work_view_limits


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def phase_root(phase):
    return ROOT / phase


def prepare(root, phase):
    root.mkdir(parents=True, exist_ok=False)
    fixtures = [case for case in cases() if case["split"] == phase]
    write(root / "fixtures.json", {"schema_version": 1, "cases": fixtures})
    write(root / "limits.json", {"turns": 18, "requests": 10, "process_seconds": 660, "agent_seconds": 600})
    write(root / "material-channel.json", {"mode": "read", "protocol": "native"})
    clock = Path("tests/fixtures/research_local_clock.txt").read_text(encoding="utf-8")
    (root / "clock-context.txt").write_text(clock.replace("2026-11-01", "2026-11-06"), encoding="utf-8")
    write(root / "preregistration.json", {
        "phase": phase, "seeds": [901] if phase == "dev" else [911, 912], "arms": ARMS,
        "work_view_cap": 4096, "provider_window": 32768,
        "pressure": "Both arms share a 4096-token experimental work-view cap; acquisition and display share it. "
                    "The real provider tokenizer and original 32768-token request admission remain active. "
                    "This stresses retention; it does not establish default 16384-token effect size.",
        "treatment": "Only on overflow, lexical original-task match/IDF and exact-text duplicate suppression "
                     "choose whole observed windows. No summaries, navigation changes or semantic reviewers.",
        "uncontrolled": "Model chooses searches, sources, offsets and submission. No seeded evidence, knowledge or tool replies.",
        "primary": "Complete task delivery AND all predeclared rubric items correct; no unsupported strong assertion.",
        "mechanism": "Actual admitted requests must lose a read critical quote under baseline; unread/always-visible are separate.",
        "gate": ["At least two effective heldout overflow pairs and a strict complete-task improvement of >=2 runs.",
                 "No new baseline-passing failure, omitted task part, condition/version error or uncited strong assertion.",
                 "All-fit exact context behavior unchanged. Repeated seeds are not independent tasks.",
                 "Only visibility improvement is not sufficient; default-budget confirmation required before promotion."],
        "gold_hash": hashlib.sha256(json.dumps(cases(), sort_keys=True).encode()).hexdigest(),
        "scoring": "Prewritten rubric, separately reviewed against original snapshots; never tested-model self-verdicts.",
    })
    write(root / "backend.json", {"model": get("/api/v1/model"), "version": get("/api/extra/version")})
    for folder in ("runs", "results", "logs", "wire"):
        (root / folder).mkdir()
    write(root / "frozen-inputs.json", {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                       for p in root.iterdir() if p.is_file()})
    freeze_experiment(root)


def get(path):
    from urllib.request import urlopen

    with urlopen(framework.UPSTREAM + path, timeout=10) as response:
        return json.load(response)


def verify(root):
    verify_experiment(root)
    for name, digest in read(root / "frozen-inputs.json").items():
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != digest:
            raise RuntimeError("Frozen evaluation file changed: " + name)


def worker(root, directory, base_url, case_id, seed, arm):
    verify(root)
    fixture = next(c for c in read(root / "fixtures.json")["cases"] if c["id"] == case_id)
    cap = read(root / "preregistration.json")["work_view_cap"]

    def record(name, row):
        with (directory / name).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")

    def limits(provider):
        counter, budget = BASE_LIMITS(provider)
        return counter, min(cap, budget)

    def view(session, ledger, scope, **kwargs):
        renders = []
        original = context._render_context

        def rendered(s, ev, selected_scope, visible):
            text = original(s, ev, selected_scope, visible)
            renders.append({"visible": [label for label, _ in visible], "text": text})
            return text

        with patch.object(context, "_render_context", rendered):
            result = (BASE_VIEW if arm == "baseline" else candidate.answer_context)(session, ledger, scope, **kwargs)
        record("retention-views.jsonl", {"budget": kwargs["token_budget"], "aliases": [list(row) for row in scope.aliases],
            "read_passages": [{"label": label, "ref": ref, "url": ledger.read_passages[ref].source.final_url,
                "source_hash": ledger.read_passages[ref].source.text_hash, "start": ledger.read_passages[ref].start,
                "end": ledger.read_passages[ref].end, "excerpt": ledger.read_passages[ref].excerpt}
                              for label, ref in scope.aliases],
            "renders": renders, "overflow": len(renders) > 1, "selected_text_hash": hashlib.sha256(result.encode()).hexdigest()})
        return result

    task = fixture["question"] + "\n只研究并交完整答案，不修改已有知识；仅使用本轮来源，不使用材料外信息。"
    with ExitStack() as stack:
        stack.enter_context(patch.object(context, "work_view_limits", limits))
        stack.enter_context(patch.object(context, "answer_context", view))
        stack.enter_context(patch.object(framework, "verify_experiment", verify))
        stack.enter_context(patch.object(framework, "task_text", lambda _root, _case: task))
        stack.enter_context(patch.object(framework, "DEFAULT_CLOCK_FILE", root / "clock-context.txt"))
        framework.codey_worker(root, directory, base_url, case_id, seed)
    verify(root)


def run_one(root, case_id, seed, arm):
    verify(root)
    ready = idle_backend()
    name = f"{case_id}-{seed}-{arm}"
    target = root / "results" / (name + ".json")
    if target.exists():
        return
    directory = root / "runs" / name
    if directory.exists():
        raise RuntimeError("Incomplete prior run retained; use a separately frozen attempt directory")
    limits = framework.experiment_limits(root)
    started = time.monotonic()
    with patch.object(framework, "verify_experiment", verify), framework.InferenceProxy(
            root / "wire" / (name + ".jsonl"), seed, genkey="retention-" + uuid4().hex,
            request_limit=limits["requests"], identity_root=root) as proxy, \
            (root / "logs" / (name + ".stdout")).open("w", encoding="utf-8") as out, \
            (root / "logs" / (name + ".stderr")).open("w", encoding="utf-8") as err:
        process = subprocess.Popen([sys.executable, "-X", "utf8", "-m", __spec__.name, "worker", "--root", str(root),
            "--directory", str(directory), "--base-url", proxy, "--case", case_id, "--seed", str(seed), "--arm", arm],
            stdout=out, stderr=err)
        timed_out = False
        try:
            process.communicate(timeout=limits["process_seconds"])
        except subprocess.TimeoutExpired:
            timed_out = True
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
            process.communicate(timeout=15)
    row = {"case": case_id, "seed": seed, "arm": arm, "process_exit": process.returncode,
           "timeout": timed_out, "seconds": round(time.monotonic() - started, 3), "backend_before": ready,
           "semantic_grade": "pending_separate_rubric_review"}
    if (directory / "research-result.json").exists():
        row["result"] = read(directory / "research-result.json")
    write(target, row)
    verify(root)
    print("END", name, row["process_exit"], row["seconds"], flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "run", "worker"))
    parser.add_argument("--phase", choices=("dev", "holdout"), default="dev")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--case")
    parser.add_argument("--seed", type=int)
    parser.add_argument("--arm", choices=ARMS)
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--base-url")
    args = parser.parse_args()
    root = (args.root or phase_root(args.phase)).resolve()
    if args.action == "prepare":
        prepare(root, args.phase)
    elif args.action == "worker":
        worker(root, args.directory, args.base_url, args.case, args.seed, args.arm)
    else:
        spec = read(root / "preregistration.json")
        for i, case in enumerate(read(root / "fixtures.json")["cases"]):
            if args.case and case["id"] != args.case:
                continue
            for seed in spec["seeds"]:
                for arm in (ARMS if (i + seed) % 2 else tuple(reversed(ARMS))):
                    if (args.seed is None or args.seed == seed) and (args.arm is None or args.arm == arm):
                        print("START", case["id"], seed, arm, flush=True)
                        run_one(root, case["id"], seed, arm)


if __name__ == "__main__":
    main()
