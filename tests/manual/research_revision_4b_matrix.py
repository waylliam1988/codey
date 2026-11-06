"""4B real submission matrix over frozen natural drafts and exact evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

from tests.manual import persistent_research_local as probe
from tests.manual.research_experiment_identity import freeze_experiment, verify_experiment

ARMS = ("U", "R", "S", "V")
SEEDS = (4301, 4302)
ORIGINALS = {
    "knowledge_wal": ("Research SQLite WAL concurrency from official original documentation. Explain concurrent readers and writers, the single-writer limit, and save one supported candidate claim with exact evidence in knowledge_updates.", "https://www.sqlite.org/wal.html"),
    "knowledge_hepatotoxicity": ("Research original PubMed evidence on immune checkpoint inhibitor hepatotoxicity management. State evidence limits and save one supported candidate claim with exact evidence in knowledge_updates.", "https://pubmed.ncbi.nlm.nih.gov/42947907/"),
    "knowledge_rag": ("Research original arXiv evidence on retrieval augmented generation evaluation. Distinguish metrics and limitations, and save one supported candidate claim with exact evidence in knowledge_updates.", "https://arxiv.org/abs/2309.15217"),
    "knowledge_react": ("Research the original ReAct paper. Explain reasoning and acting, evaluation scope and limitations, and save one supported candidate claim with exact evidence in knowledge_updates.", "https://arxiv.org/abs/2210.03629"),
}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def source_excerpt(root: Path, case: str) -> tuple[str, str]:
    base = Path(".e2e-artifacts/research-delivery-4a-formal-v2") / f"{case}-4201-baseline"
    draft = (base / "research-report.txt").read_text(encoding="utf-8")
    ledgers = list((base / "state" / "research" / "evidence_ledgers").rglob("no_project.json"))
    excerpts = []
    if ledgers:
        ledger = json.loads(ledgers[0].read_text(encoding="utf-8"))
        excerpts = [item.get("bounded_excerpt", "") for item in ledger.get("evidence", {}).values() if item.get("bounded_excerpt")]
    return draft, "\n\n".join(excerpts) or "The frozen run had no settled evidence excerpt."


def prepare(root: Path, base_url: str) -> None:
    root.mkdir(parents=True, exist_ok=False)
    fixtures = {}
    for case, (question, url) in ORIGINALS.items():
        draft, excerpt = source_excerpt(root, case)
        # The frozen packet contains one exact source excerpt; normalize the
        # natural draft's multiple historical labels to that single readable
        # evidence label before freezing the 4B input.
        draft = re.sub(r"\[(?:e\d+)(?:\s*,\s*e\d+)*\]", "[e1]", draft)
        fixtures[case] = {"question": question, "draft": draft, "url": url, "evidence": excerpt,
                          "draft_digest": hashlib.sha256(draft.encode()).hexdigest()}
    with urlopen(base_url + "/v1/models", timeout=10) as response:
        backend = json.load(response)
    with urlopen(base_url + "/api/extra/version", timeout=10) as response:
        version = json.load(response)
    contract = {
        "schema_version": 1, "experiment": "4B", "phase": "dev", "arms": {"names": ARMS, "treatments": {"U": "frozen original draft direct submission", "R": "one direct reread-and-revise call", "S": "self-check then revise", "V": "independent evidence review then revise"}}, "seeds": SEEDS,
        "tasks": len(fixtures), "model": backend["data"][0]["id"], "backend_version": version,
        "design": "One frozen natural draft and one frozen exact evidence packet are reused across U/R/S/V. Each arm executes real source reads, submit_research_answer, validation and isolated atomic publication.",
        "gate": {"positive_tasks_min": 3, "negative_tasks_max": 1, "strict_success_delta_min": 0.10, "new_severe_regressions_max": 0, "cost_ratio_max": 1.25},
        "review": "Final answers are source-reviewed against the original task and frozen evidence; reviewer text is not gold.",
    }
    write(root / "fixtures.json", fixtures)
    write(root / "preregistration.json", contract)
    write(root / "contract-hash.json", {"sha256": hashlib.sha256((root / "preregistration.json").read_bytes()).hexdigest()})
    freeze_experiment(root)


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def verify(root):
    verify_experiment(root)
    if hashlib.sha256((root / "preregistration.json").read_bytes()).hexdigest() != read(root / "contract-hash.json")["sha256"]:
        raise RuntimeError("frozen contract changed")


def worker(root: Path, case: str, arm: str, seed: int) -> None:
    verify(root)
    fixture = read(root / "fixtures.json")[case]
    directory = root / "runs" / f"{case}-{seed}-{arm}"
    directory.mkdir(parents=True, exist_ok=False)
    source_urls = [fixture["url"]]
    if fixture["url"] == "https://www.sqlite.org/wal.html":
        source_urls.append("https://sqlite.org/wal.html")
    sources = [{"url": url, "title": "Frozen source evidence", "text": fixture["evidence"]} for url in source_urls]
    (directory / "sources.json").write_text(json.dumps(sources, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    instructions = {
        "U": "Submit the frozen draft exactly after reading the supplied source. Do not rewrite it.",
        "R": "Reread the original task, frozen draft and exact source, then revise once before submitting. Preserve supported content and repair only verified issues.",
        "S": "Perform a self-check against the original task and exact source, then revise once before submitting. Do not invent claims.",
        "V": "Act as an independent evidence reviewer first, then revise once using only verified feedback and the exact source. Do not trust unsupported feedback.",
    }
    question = (fixture["question"] + "\n\nFrozen original draft:\n" + fixture["draft"] +
                "\n\nFrozen evidence packet is in the supplied source. " + instructions[arm] +
                " Use the real research tools, submit the complete answer, and preserve knowledge_updates when required.")
    probe.stage(directory, "research", "http://127.0.0.1:5001", read(root / "preregistration.json")["model"],
                native=False, live_web=False, question=question, max_turns=12, seed=seed,
                provider_timeout=180, constrained_text=True)


def run(root):
    verify(root)
    contract = read(root / "preregistration.json")
    for case in read(root / "fixtures.json"):
        for seed in contract["seeds"]:
            for arm in ARMS:
                name = f"{case}-{seed}-{arm}"
                receipt = root / (name + ".json")
                if receipt.exists():
                    continue
                started = time.monotonic()
                command = [sys.executable, "-m", "tests.manual.research_revision_4b_matrix", "worker",
                           "--root", str(root), "--case", case, "--arm", arm, "--seed", str(seed)]
                result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
                write(receipt, {"exit_code": result.returncode, "stdout": result.stdout, "stderr": result.stderr,
                                "elapsed_seconds": time.monotonic() - started})
                print(name, result.returncode, flush=True)
                if result.returncode:
                    raise RuntimeError("retained 4B failure; stop: " + name)


def main():
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
