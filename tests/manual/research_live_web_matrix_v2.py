"""2A development matrix with retained task failures and shared backend gate."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from tests.manual import research_live_web_matrix as base
from tests.manual.research_experiment_identity import verify_experiment


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def prepare(root: Path, base_url: str):
    base.prepare(root, "dev", base_url)
    contract = json.loads((root / "preregistration.json").read_text(encoding="utf-8"))
    contract["experiment"] = "2A-v2"
    contract["seeds"] = [1501]
    contract["failure_policy"] = (
        "Task-level no_progress, repeated reads and incomplete answer are retained as failed runs and the matrix continues. "
        "Stop only for backend identity change, schema/compiler failure, protocol/infrastructure failure or process timeout."
    )
    (root / "preregistration.json").write_text(json.dumps(contract, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write(root / "contract-hash.json", {"sha256": hashlib.sha256((root / "preregistration.json").read_bytes()).hexdigest()})
    # The base preparation already froze source identity; the new contract is
    # independently hashed above and this directory is never resumed after a
    # source change.


def verify(root):
    verify_experiment(root)
    contract = json.loads((root / "preregistration.json").read_text(encoding="utf-8"))
    digest = json.loads((root / "contract-hash.json").read_text(encoding="utf-8"))["sha256"]
    if hashlib.sha256((root / "preregistration.json").read_bytes()).hexdigest() != digest:
        raise RuntimeError("2A v2 contract changed")
    return contract


def run(root):
    contract = verify(root)
    cases = json.loads((root / "fixtures.json").read_text(encoding="utf-8"))
    for case in cases:
        for seed in contract["seeds"]:
            for arm in base.live.ARMS:
                name = f"{case}-{seed}-{arm}"
                receipt = root / f"{name}.json"
                if receipt.exists():
                    continue
                command = [sys.executable, "-m", "tests.manual.research_live_web_matrix_v2", "worker",
                           "--root", str(root), "--case", case, "--arm", arm, "--seed", str(seed)]
                result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                        timeout=contract["process_timeout_seconds"])
                failure = None if result.returncode == 0 else "task_failure"
                write(receipt, {"exit_code": result.returncode, "failure_class": failure,
                                "stdout": result.stdout, "stderr": result.stderr})
                print(name, result.returncode, flush=True)
                if result.returncode and any(token in result.stderr.lower() for token in (
                        "backend identity changed", "grammar", "protocol", "connection refused", "timeout")):
                    raise RuntimeError("shared infrastructure failure; stop: " + name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("prepare", "run", "worker"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:5001")
    parser.add_argument("--case")
    parser.add_argument("--arm", choices=base.live.ARMS)
    parser.add_argument("--seed", type=int)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.command == "prepare":
        prepare(root, args.base_url)
    elif args.command == "run":
        run(root)
    else:
        contract = verify(root)
        base.live.QUESTIONS.update(json.loads((root / "fixtures.json").read_text(encoding="utf-8")))
        base.live.run(root, args.case, args.arm, args.seed, contract["base_url"], contract["model"],
                      constrained_text=contract["constrained_text"])


if __name__ == "__main__":
    main()
