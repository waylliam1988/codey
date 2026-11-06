"""Fresh natural-pressure development contract for coverage retention."""
from __future__ import annotations

import argparse
from pathlib import Path

from tests.manual import research_window_coverage_ab_v2 as v2

# These holdout fixtures were not used by v12.  They provide natural long
# records and distinct version/object/exception facets without padding the
# task after the fact.
CASE_IDS = ("held_versions", "held_middle", "held_decoy", "held_logic")


def prepare(root: Path, base_url: str) -> None:
    v2.DEV_IDS = CASE_IDS
    v2.prepare(root, base_url)
    contract = v2.json.loads((root / "preregistration.json").read_text(encoding="utf8"))
    contract["phase"] = "dev-v13-natural-pressure"
    contract["seeds"] = [1702]
    contract["cases"] = list(CASE_IDS)
    contract["gate"] = "At least two paired real-writer overflows; otherwise inconclusive."
    v2.write(root / "preregistration.json", contract)
    v2.write(root / "contract-hash.json", {"sha256": v2.hashlib.sha256((root / "preregistration.json").read_bytes()).hexdigest()})
    frozen = v2.json.loads((root / "frozen-inputs.json").read_text(encoding="utf8"))
    frozen.pop("contract-hash.json", None)
    frozen.pop("frozen-inputs.json", None)
    frozen["preregistration.json"] = v2.hashlib.sha256((root / "preregistration.json").read_bytes()).hexdigest()
    v2.write(root / "frozen-inputs.json", frozen)
    v2.freeze_experiment(root)


def run(root: Path) -> None:
    v2.DEV_IDS = CASE_IDS
    v2.run(root)


def main() -> None:
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
