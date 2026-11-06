"""Real-writer development A/B for coverage-preserving context eviction."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from urllib.request import urlopen

from tests.manual import research_window_coverage_candidate as coverage
from tests.manual import research_window_retention_ab as base
from tests.manual.research_experiment_identity import freeze_experiment, verify_experiment
from tests.manual.research_window_retention_cases import cases

DEV_IDS = ("dev_early", "held_early", "held_decoy", "held_middle")
ARMS = ("baseline", "coverage")


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf8")


def prepare(root: Path, base_url: str):
    root.mkdir(parents=True, exist_ok=False)
    selected = [case for case in cases() if case["id"] in DEV_IDS]
    write(root / "fixtures.json", {"schema_version": 1, "cases": selected})
    write(root / "limits.json", {"turns": 18, "requests": 10, "process_seconds": 660, "agent_seconds": 600})
    write(root / "material-channel.json", {"mode": "read", "protocol": "plain"})
    clock = Path("tests/fixtures/research_local_clock.txt").read_text(encoding="utf8")
    (root / "clock-context.txt").write_text(clock.replace("2026-11-01", "2026-11-07"), encoding="utf8")
    contract = {"phase": "dev-v2", "seeds": [1701], "arms": ARMS, "cases": DEV_IDS,
                "work_view_cap": 4096, "provider_window": 32768,
                "treatment": "Only overflow eviction changes: preserve object/version/limit facets and remove redundant windows first.",
                "mechanism": "Both arms must admit the same task with actual overflow; selected windows and read receipts are retained.",
                "gate": "At least two paired real-writer overflows; no production change; semantic quality scored separately."}
    with urlopen(base.framework.UPSTREAM + "/api/v1/model", timeout=10) as response:
        contract["model"] = json.load(response)
    write(root / "preregistration.json", contract)
    write(root / "contract-hash.json", {"sha256": hashlib.sha256((root / "preregistration.json").read_bytes()).hexdigest()})
    write(root / "backend.json", {"model": contract["model"]})
    for name in ("runs", "results", "logs", "wire"):
        (root / name).mkdir()
    write(root / "frozen-inputs.json", {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                       for p in root.iterdir() if p.is_file()})
    freeze_experiment(root)


def verify(root):
    verify_experiment(root)
    for name, digest in json.loads((root / "frozen-inputs.json").read_text(encoding="utf8")).items():
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != digest:
            raise RuntimeError("Frozen coverage input changed: " + name)
    digest = hashlib.sha256((root / "preregistration.json").read_bytes()).hexdigest()
    if not (root / "contract-hash.json").exists():
        write(root / "contract-hash.json", {"sha256": digest})
    elif json.loads((root / "contract-hash.json").read_text(encoding="utf8"))["sha256"] != digest:
        raise RuntimeError("Coverage contract changed")


def configure():
    base.candidate = coverage
    base.ARMS = ARMS
    base.verify = verify


def run(root):
    configure()
    verify(root)
    contract = json.loads((root / "preregistration.json").read_text(encoding="utf8"))
    for case in contract["cases"]:
        for seed in contract["seeds"]:
            for arm in ARMS:
                base.run_one(root, case, seed, arm)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("prepare", "run"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:5001")
    args = parser.parse_args()
    root = args.root.resolve()
    if args.command == "prepare":
        prepare(root, args.base_url)
    else:
        run(root)


if __name__ == "__main__":
    main()
