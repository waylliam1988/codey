"""Independent 2B live A/B: one public full-text recovery path after abstracts."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from urllib.request import urlopen

from tests.manual import research_live_web_local_ab as live
from tests.manual.research_experiment_identity import freeze_experiment, verify_experiment

spec = importlib.util.spec_from_file_location("fulltext_draft", Path(".e2e-artifacts/research-next-candidate-drafts/fulltext_live.py"))
assert spec and spec.loader
draft = importlib.util.module_from_spec(spec)
spec.loader.exec_module(draft)

TOPICS = {
    "arxiv_attention": "arXiv Attention Is All You Need: architecture, training objective, evaluation and limitations",
    "arxiv_bert": "arXiv BERT: pretraining objectives, evaluation tasks, reported limits and scope",
    "arxiv_t5": "arXiv T5: text-to-text setup, pretraining, evaluation and limitations",
    "arxiv_dpr": "arXiv DPR: retrieval training, benchmarks, comparison and limitations",
    "arxiv_realm": "arXiv REALM: retrieval during pretraining, evaluation and limitations",
    "pubmed_recovery": "PubMed evidence on RECOVERY trial methods, outcomes and limitations",
    "pubmed_sprint": "PubMed evidence on SPRINT trial methods, outcomes and limitations",
    "pubmed_step1": "PubMed evidence on STEP 1 semaglutide trial methods, outcomes and limitations",
}


def write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def prepare(root: Path, base_url: str) -> None:
    root.mkdir(parents=True, exist_ok=False)
    with urlopen(base_url + "/v1/models", timeout=10) as response:
        backend = json.load(response)
    with urlopen(base_url + "/api/extra/version", timeout=10) as response:
        version = json.load(response)
    contract = {
        "schema_version": 1, "experiment": "2B", "phase": "formal",
        "arms": live.ARMS, "seeds": [2301, 2302], "model": backend["data"][0]["id"],
        "backend_version": version, "base_url": base_url, "context_window": 32768,
        "output_tokens": live.OUTPUT_TOKENS, "turns": 12,
        "protocol": "production constrained JSON; enable_thinking=false; reasoning_effort=none; temperature=0.3",
        "treatment": "Only after a production abstract fetch, attempt one identity-safe public full-text route; fallback is exact abstract.",
        "routes": "arXiv abs to same-version PDF; PubMed ELink pubmed_pmc to PMC; no new search ranking or prompts.",
        "primary": "complete task correctness with exact read evidence and preserved limitations",
        "mechanism": "at least one eligible abstract and one attempted/recovered public full-text route in each activated pair",
        "gate": {"positive_tasks_min": 3, "negative_tasks_max": 1, "strict_success_delta_min": 0.10,
                 "new_severe_regressions_max": 0, "token_and_latency_ratio_max": 1.25,
                 "confirm_new_tasks_required": True},
        "failures": "retain every attempt; stop frozen matrix on first failure; untriggered recovery is inconclusive",
    }
    write(root / "fixtures.json", {key: "Research " + value + ". Read original sources, answer in Chinese, cite exact [eN] evidence, and state unresolved points." for key, value in TOPICS.items()})
    write(root / "preregistration.json", contract)
    write(root / "contract-hash.json", {"sha256": hashlib.sha256((root / "preregistration.json").read_bytes()).hexdigest()})
    freeze_experiment(root)


def run(root: Path) -> None:
    verify_experiment(root)
    contract = json.loads((root / "preregistration.json").read_text(encoding="utf-8"))
    if hashlib.sha256((root / "preregistration.json").read_bytes()).hexdigest() != json.loads((root / "contract-hash.json").read_text())["sha256"]:
        raise RuntimeError("Frozen contract changed")
    live.QUESTIONS.update(json.loads((root / "fixtures.json").read_text(encoding="utf-8")))
    original = live.BrowserAfterConnector
    live.BrowserAfterConnector = draft.PublicFulltextRecovery
    try:
        for index, case in enumerate(TOPICS):
            for seed in contract["seeds"]:
                arms = contract["arms"] if (index + seed) % 2 else list(reversed(contract["arms"]))
                for arm in arms:
                    name = f"{case}-{seed}-{arm}"
                    receipt = root / (name + ".json")
                    if receipt.exists():
                        continue
                    if (root / name).exists():
                        raise RuntimeError("unfinished attempt retained: " + name)
                    command = [sys.executable, "-m", "tests.manual.research_live_fulltext_matrix", "worker",
                               "--root", str(root), "--case", case, "--arm", arm, "--seed", str(seed)]
                    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
                    write(receipt, {"exit_code": result.returncode, "stdout": result.stdout, "stderr": result.stderr})
                    print(name, result.returncode, flush=True)
                    if result.returncode:
                        raise RuntimeError("retained failed run; stop: " + name)
    finally:
        live.BrowserAfterConnector = original


def worker(root: Path, case: str, arm: str, seed: int) -> None:
    verify_experiment(root)
    contract = json.loads((root / "preregistration.json").read_text(encoding="utf-8"))
    live.QUESTIONS.update(json.loads((root / "fixtures.json").read_text(encoding="utf-8")))
    import socket
    with socket.socket() as lock:
        lock.bind(("127.0.0.1", 15001))
        live.BrowserAfterConnector = draft.PublicFulltextRecovery
        live.run(root, case, arm, seed, contract["base_url"], contract["model"], constrained_text=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("prepare", "run", "worker"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--case")
    parser.add_argument("--arm", choices=live.ARMS)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--base-url", default="http://127.0.0.1:5001")
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.root.resolve(), args.base_url)
    elif args.command == "run":
        run(args.root.resolve())
    else:
        worker(args.root.resolve(), args.case, args.arm, args.seed)


if __name__ == "__main__":
    main()
