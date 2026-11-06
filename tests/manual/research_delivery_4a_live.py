"""4A development A/B: add only the pre-submit delivery completeness prompt."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen

from tests.manual import research_live_web_local_ab as live
from tests.manual.research_delivery_candidates import DELIVERY_PROMPT

CASES = {
    "knowledge": "Research a current topic with original sources. Answer in Chinese with exact citations and save one supported candidate claim in knowledge_updates with its evidence.",
    "comparison": "Research and compare two versions or methods. Answer every comparison point, preserve limitations, and cite exact original evidence in Chinese.",
    "ordinary": "Research the requested topic from original sources and provide a concise Chinese answer with exact citations and uncertainty.",
    "knowledge_two": "Research the requested topic, answer all explicit points in Chinese, and save two distinct supported candidate claims with exact evidence in knowledge_updates.",
}


def worker(root: Path, case: str, arm: str, seed: int) -> None:
    live.QUESTIONS.update(CASES)
    original_class = live.BrowserAfterConnector
    live.BrowserAfterConnector = live.ObservedProduction
    try:
        from codey.research import answer_context
        with patch.object(answer_context, "ANSWER_GUIDANCE", answer_context.ANSWER_GUIDANCE + ("\n\n" + DELIVERY_PROMPT if arm == "delivery" else "")):
            with urlopen("http://127.0.0.1:5001/v1/models", timeout=10) as response:
                model = json.load(response)["data"][0]["id"]
            live.run(root, case, arm=("baseline" if arm == "baseline" else "browser_after_connector"), seed=seed,
                     base_url="http://127.0.0.1:5001", model=model, constrained_text=True)
    finally:
        live.BrowserAfterConnector = original_class


def run_matrix(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=False)
    (root / "fixtures.json").write_text(json.dumps(CASES, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for case in CASES:
        for seed in (4101,):
            for arm in ("baseline", "delivery"):
                name = f"{case}-{seed}-{arm}"
                result = subprocess.run([sys.executable, "-m", __spec__.name, "--root", str(root), "--case", case, "--arm", arm, "--seed", str(seed)], capture_output=True, text=True, encoding="utf-8")
                (root / (name + ".json")).write_text(json.dumps({"exit_code": result.returncode, "stdout": result.stdout, "stderr": result.stderr}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                if result.returncode:
                    raise RuntimeError("4A development run failed: " + name)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("run", "worker"), default="worker", nargs="?")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--case", choices=tuple(CASES))
    parser.add_argument("--arm", choices=("baseline", "delivery"))
    parser.add_argument("--seed", type=int)
    args = parser.parse_args()
    if args.command == "run":
        run_matrix(args.root.resolve())
    else:
        if args.case is None or args.arm is None or args.seed is None:
            parser.error("worker requires --case, --arm and --seed")
        worker(args.root.resolve(), args.case, args.arm, args.seed)


if __name__ == "__main__":
    main()
