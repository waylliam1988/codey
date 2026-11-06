"""Frozen 4A live matrix: pre-submit completeness prompt only."""
from __future__ import annotations

import argparse
import hashlib
import json
import socket
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen

from tests.manual import research_live_web_local_ab as live
from tests.manual.research_delivery_candidates import DELIVERY_PROMPT
from tests.manual.research_experiment_identity import freeze_experiment, verify_experiment

ARMS = ("baseline", "delivery")
SEEDS = (4201, 4202)
CASES = {
    "knowledge_wal": "Research SQLite WAL concurrency from official original documentation. Explain concurrent readers and writers, the single-writer limit, and save one supported candidate claim with exact evidence in knowledge_updates.",
    "knowledge_hepatotoxicity": "Research original PubMed evidence on immune checkpoint inhibitor hepatotoxicity management. State evidence limits and save one supported candidate claim with exact evidence in knowledge_updates.",
    "knowledge_rag": "Research original arXiv evidence on retrieval augmented generation evaluation. Distinguish metrics and limitations, and save one supported candidate claim with exact evidence in knowledge_updates.",
    "knowledge_react": "Research the original ReAct paper. Explain reasoning and acting, evaluation scope and limitations, and save one supported candidate claim with exact evidence in knowledge_updates.",
    "compare_bert_t5": "Compare BERT and T5 original papers on pretraining objectives, input/output formulation, evaluation and limitations. Cite each source and preserve distinctions.",
    "compare_attention_recurrence": "Compare the Attention Is All You Need paper with recurrent sequence models on architecture, training parallelism, evaluation and limits. Cite original evidence.",
    "compare_ragas": "Compare faithfulness and answer relevance in the original RAGAS paper, including inputs, evaluation scope and limitations. Cite exact evidence.",
    "compare_retrieval": "Compare DPR and REALM original papers on retrieval training, pretraining role, benchmarks and limitations. Do not mix their settings.",
    "ordinary_myocarditis": "Research original PubMed evidence on checkpoint inhibitor myocarditis management, outcomes and limitations. Answer in Chinese with exact citations.",
    "ordinary_long_context": "Research original arXiv evidence on long-context language-model evaluation limits. Cite exact evidence and state unresolved points.",
    "ordinary_sicca": "Research original PubMed evidence on checkpoint inhibitor sicca syndrome diagnosis, treatment, reversibility and limitations.",
    "ordinary_ragtruth": "Research the original RAGTruth paper on hallucination annotation, data scope, evaluation and limitations with exact citations.",
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
        "schema_version": 1, "experiment": "4A", "phase": "formal", "arms": ARMS,
        "seeds": SEEDS, "tasks": 12, "model": backend["data"][0]["id"], "backend_version": version,
        "base_url": base_url, "protocol": "production constrained JSON; no extra model call",
        "treatment": "append one short pre-submit completeness instruction to the existing answer guidance",
        "primary": "complete task delivery, exact evidence support, and requested knowledge_updates saved atomically",
        "secondary": "ordinary tasks do not gain unsolicited knowledge_updates; no correct content is removed",
        "gate": {"positive_tasks_min": 3, "negative_tasks_max": 1, "strict_success_delta_min": 0.10,
                 "new_severe_regressions_max": 0, "ordinary_regressions_max": 0,
                 "token_ratio_max": 1.25, "confirm_new_tasks_required": True},
        "failures": "retain every attempt; stop on protocol/backend/source identity failure; task-level writer failure is scored as failed run and the healthy matrix continues",
    }
    write(root / "fixtures.json", CASES)
    write(root / "preregistration.json", contract)
    write(root / "contract-hash.json", {"sha256": hashlib.sha256((root / "preregistration.json").read_bytes()).hexdigest()})
    freeze_experiment(root)


def read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def verify(root: Path) -> None:
    verify_experiment(root)
    expected = read(root / "contract-hash.json")["sha256"]
    actual = hashlib.sha256((root / "preregistration.json").read_bytes()).hexdigest()
    if expected != actual:
        raise RuntimeError("frozen contract changed")


def worker(root: Path, case: str, arm: str, seed: int) -> None:
    verify(root)
    live.QUESTIONS.update(read(root / "fixtures.json"))
    from codey.research import answer_context

    original_class = live.BrowserAfterConnector
    live.BrowserAfterConnector = live.ObservedProduction
    try:
        with patch.object(answer_context, "ANSWER_GUIDANCE",
                          answer_context.ANSWER_GUIDANCE + ("\n\n" + DELIVERY_PROMPT if arm == "delivery" else "")):
            with urlopen("http://127.0.0.1:5001/v1/models", timeout=10) as response:
                model = json.load(response)["data"][0]["id"]
            with socket.socket() as lock:
                lock.bind(("127.0.0.1", 15001))
                live.run(root, case, "baseline" if arm == "baseline" else "browser_after_connector", seed,
                         "http://127.0.0.1:5001", model, constrained_text=True)
    finally:
        live.BrowserAfterConnector = original_class


def run(root: Path) -> None:
    verify(root)
    contract = read(root / "preregistration.json")
    cases = read(root / "fixtures.json")
    for index, case in enumerate(cases):
        for seed in contract["seeds"]:
            arms = ARMS if (index + seed) % 2 else tuple(reversed(ARMS))
            for arm in arms:
                name = f"{case}-{seed}-{arm}"
                receipt = root / (name + ".json")
                if receipt.exists():
                    continue
                if (root / name).exists():
                    raise RuntimeError("unfinished attempt retained: " + name)
                started = time.monotonic()
                command = [sys.executable, "-m", "tests.manual.research_delivery_4a_matrix", "worker",
                           "--root", str(root), "--case", case, "--arm", arm, "--seed", str(seed)]
                result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
                write(receipt, {"exit_code": result.returncode, "stdout": result.stdout, "stderr": result.stderr,
                                "elapsed_seconds": time.monotonic() - started})
                print(name, result.returncode, flush=True)
                if result.returncode and any(token in result.stderr.lower() for token in ("grammar", "backend identity", "evaluation source changed", "frozen contract changed")):
                    raise RuntimeError("retained infrastructure failure; stop: " + name)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("prepare", "run", "worker"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:5001")
    parser.add_argument("--case")
    parser.add_argument("--arm", choices=ARMS)
    parser.add_argument("--seed", type=int)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.command == "prepare":
        prepare(root, args.base_url)
    elif args.command == "run":
        run(root)
    else:
        if args.case is None or args.arm is None or args.seed is None:
            parser.error("worker requires case, arm and seed")
        worker(root, args.case, args.arm, args.seed)


if __name__ == "__main__":
    main()
