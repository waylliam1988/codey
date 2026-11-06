"""Freeze and run real-network 2A matrices without silently retrying failures.

prepare creates a new immutable contract; run verifies it before every sample.
Semantic ratings are separate reviewed data, never inferred from tool success.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from urllib.request import urlopen

from tests.manual import research_live_web_local_ab as live
from tests.manual.research_experiment_identity import freeze_experiment, verify_experiment

DEV_CASES = tuple(live.QUESTIONS)[:4]
FORMAL_TOPICS = (
    ("pubmed_pneumonitis", "PubMed evidence on checkpoint inhibitor pneumonitis: diagnostic exclusion, initial management, escalation and evidence limitations"),
    ("pubmed_colitis", "PubMed evidence on checkpoint inhibitor colitis: diagnosis, initial treatment, refractory disease and evidence limitations"),
    ("pubmed_arthritis", "PubMed evidence on checkpoint inhibitor inflammatory arthritis: diagnostic scope, treatment, persistence after cessation and limitations"),
    ("pubmed_neuropathy", "PubMed evidence on checkpoint inhibitor peripheral neuropathy: differential diagnosis, management, outcome and limitations"),
    ("pubmed_pancreatitis", "PubMed evidence on checkpoint inhibitor pancreatic injury: distinguish asymptomatic enzyme elevation from pancreatitis, management and limitations"),
    ("pubmed_rechallenge", "PubMed evidence on checkpoint inhibitor rechallenge after severe adverse events: recurrence, selection conditions, study design and limitations"),
    ("arxiv_lost_middle", "arXiv evidence on Lost in the Middle: compare document positions, evaluated tasks, model scope and limits of generalization"),
    ("arxiv_self_rag", "arXiv evidence on Self-RAG: retrieval decisions, critique tokens, evaluated tasks and limitations"),
    ("arxiv_ragas", "arXiv evidence on RAGAS: distinguish faithfulness and answer relevance, required inputs, evaluation scope and limitations"),
    ("arxiv_react", "arXiv evidence on ReAct: reasoning and acting, experimental settings, reported comparisons and limitations"),
    ("arxiv_needle", "arXiv evidence comparing needle-in-a-haystack tests with realistic long-context benchmarks: tasks, distractors, scoring and limits"),
    ("arxiv_ragtruth", "arXiv evidence on RAGTruth: hallucination annotation granularity, data scope, evaluation and limitations"),
)
CONFIRM_TOPICS = (
    ("pubmed_myositis", "PubMed evidence on checkpoint inhibitor myositis: overlap syndrome, diagnosis, treatment and evidence limitations"),
    ("pubmed_cardiac_rechallenge", "PubMed evidence on rechallenge after checkpoint inhibitor myocarditis: selection, recurrence, study design and uncertainty"),
    ("pubmed_hypophysitis", "PubMed evidence on checkpoint inhibitor hypophysitis: endocrine findings, treatment, recovery and limitations"),
    ("pubmed_diabetes", "PubMed evidence on checkpoint inhibitor diabetes: presentation, management, reversibility and limitations"),
    ("arxiv_crag", "arXiv evidence on Corrective Retrieval Augmented Generation: retrieval evaluation, corrective actions, experimental scope and limitations"),
    ("arxiv_flare", "arXiv evidence on FLARE active retrieval: retrieval triggers, generation process, evaluated tasks and limitations"),
    ("arxiv_alce", "arXiv evidence on ALCE: citation correctness, citation completeness, evaluation datasets and limitations"),
    ("arxiv_infinitebench", "arXiv evidence on InfiniteBench: context lengths, task types, scoring and limits of generalization"),
)


def fixtures(phase: str) -> dict[str, str]:
    if phase == "dev":
        return {key: live.QUESTIONS[key] for key in DEV_CASES}
    topics = FORMAL_TOPICS if phase == "formal" else CONFIRM_TOPICS
    return {key: "Research " + topic + ". Read original sources. Answer every listed point in Chinese with exact [eN] citations. "
            "Distinguish source findings from inference, preserve conditions and uncertainty, and state any unresolved point."
            for key, topic in topics}


def write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def prepare(root: Path, phase: str, base_url: str) -> None:
    root.mkdir(parents=True, exist_ok=False)
    with urlopen(base_url + "/v1/models", timeout=10) as response:
        backend = json.load(response)
    with urlopen(base_url + "/api/extra/version", timeout=10) as response:
        version = json.load(response)
    contract = {
        "schema_version": 1, "experiment": "2A", "phase": phase,
        "arms": live.ARMS, "seeds": [1201] if phase == "dev" else [1301, 1302] if phase == "formal" else [1401, 1402],
        "model": backend["data"][0]["id"], "backend_version": version,
        "base_url": base_url, "context_window": 32768, "output_tokens": live.OUTPUT_TOKENS,
        "turns": 12, "provider_timeout_seconds": 180, "process_timeout_seconds": 900,
        "protocol": "production constrained JSON tool calls; enable_thinking=false AND reasoning_effort=none; temperature 0.3",
        "constrained_text": True,
        "sole_treatment": "remove connector hit-count early stop; preserve browser error fallback, cancellation, merge and dedup",
        "network": "live connector and browser; original retrieved snapshots and actual wire retained per run; SSL verification disabled in both development adapters",
        "scoring": {
            "strict_success": "all requested facets answered correctly, important claims supported by actually read exact sources, limitations preserved, no severe scope/citation error, answer atomically saved",
            "per_facet": "review original task and actual source snapshots; score each requested facet correct/incorrect/missing; invented certainty fails",
            "review": "independent source-based assessment; writer declarations and keyword counts are not gold",
            "ordinary_tasks": (["arxiv_self_rag", "arxiv_react", "arxiv_ragas", "arxiv_ragtruth"]
                               if phase != "confirm" else [name for name, _ in CONFIRM_TOPICS if name.startswith("arxiv_")]),
        },
        "mechanism": "at least two connector hits and suppressed browser in baseline; browser results returned to writer in candidate; report triggered subset and all runs",
        "gate": {"positive_tasks_min": 3, "negative_tasks_max": 1, "strict_success_delta_min": 0.10,
                 "new_severe_regressions_max": 0, "ordinary_regressions_max": 0,
                 "token_and_latency_ratio_max": 1.25, "confirm_new_tasks_required": True},
        "failures": "retain every attempt; no silent retry or exclusion; stop matrix on shared backend generation degeneration; untriggered mechanism is inconclusive",
    }
    write(root / "fixtures.json", fixtures(phase))
    write(root / "preregistration.json", contract)
    write(root / "contract-hash.json", {"sha256": hashlib.sha256((root / "preregistration.json").read_bytes()).hexdigest()})
    freeze_experiment(root)


def verify(root: Path) -> dict:
    verify_experiment(root)
    digest = json.loads((root / "contract-hash.json").read_text(encoding="utf-8"))["sha256"]
    if hashlib.sha256((root / "preregistration.json").read_bytes()).hexdigest() != digest:
        raise RuntimeError("Frozen contract changed")
    return json.loads((root / "preregistration.json").read_text(encoding="utf-8"))


def run(root: Path) -> None:
    contract = verify(root)
    cases = json.loads((root / "fixtures.json").read_text(encoding="utf-8"))
    for index, case in enumerate(cases):
        for seed in contract["seeds"]:
            arms = contract["arms"] if (index + seed) % 2 else list(reversed(contract["arms"]))
            for arm in arms:
                verify(root)
                name = f"{case}-{seed}-{arm}"
                receipt = root / (name + ".json")
                if receipt.exists():
                    continue
                if (root / name).exists():
                    raise RuntimeError("Unfinished attempt retained; inspect it before a new frozen matrix: " + name)
                command = [sys.executable, "-m", "tests.manual.research_live_web_matrix", "worker",
                           "--root", str(root), "--case", case, "--arm", arm, "--seed", str(seed)]
                try:
                    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                            errors="replace", timeout=contract["process_timeout_seconds"])
                    write(receipt, {"exit_code": result.returncode, "stdout": result.stdout, "stderr": result.stderr})
                except subprocess.TimeoutExpired:
                    write(receipt, {"exit_code": None, "failure": "process_timeout"})
                    raise
                print(name, result.returncode, flush=True)
                if result.returncode:
                    raise RuntimeError("Retained failed run; stop for root cause assessment: " + name)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("prepare", "run", "worker"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--phase", choices=("dev", "formal", "confirm"), default="dev")
    parser.add_argument("--base-url", default="http://127.0.0.1:5001")
    parser.add_argument("--case")
    parser.add_argument("--arm", choices=live.ARMS)
    parser.add_argument("--seed", type=int)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.command == "prepare":
        prepare(root, args.phase, args.base_url)
    elif args.command == "run":
        run(root)
    else:
        contract = verify(root)
        with urlopen(contract["base_url"] + "/v1/models", timeout=10) as response:
            model = json.load(response)["data"][0]["id"]
        with urlopen(contract["base_url"] + "/api/extra/version", timeout=10) as response:
            version = json.load(response)
        if model != contract["model"] or version != contract["backend_version"]:
            raise RuntimeError("Frozen backend identity changed; do not run this sample")
        live.QUESTIONS.update(json.loads((root / "fixtures.json").read_text(encoding="utf-8")))
        with live.socket.socket() as lock:
            if hasattr(live.socket, "SO_EXCLUSIVEADDRUSE"):
                lock.setsockopt(live.socket.SOL_SOCKET, live.socket.SO_EXCLUSIVEADDRUSE, 1)
            lock.bind(("127.0.0.1", 15001))
            live.run(root, args.case, args.arm, args.seed, contract["base_url"], contract["model"],
                     constrained_text=contract["constrained_text"])


if __name__ == "__main__":
    main()
