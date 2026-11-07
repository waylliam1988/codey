"""Formal two-arm matrix for coverage-preserving context retention."""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path

from tests.manual import research_window_coverage_ab_v2 as v2
from tests.manual.research_window_retention_cases import cases as source_cases

SEEDS = (1711, 1712)
TEMPLATES = ("held_versions", "held_middle", "held_decoy", "held_logic", "held_early", "held_fit")
NAMES = (
    ("Vela", "Kite", "Kite 1", "Kite 2"),
    ("Tarin M6", "Orca N4", "Orca N4"),
    ("Zerin L3", "Pine R8", "Pine R8"),
    ("Seno Q7", "Lumen H2", "Lumen H2"),
    ("Iven V4", "Cedar X5", "Cedar X5"),
    ("Mavo T5", "Quartz J6", "Quartz J6"),
)


def _replace(value, replacements):
    if isinstance(value, str):
        for old, new in replacements:
            value = value.replace(old, new)
        return value
    if isinstance(value, list):
        return [_replace(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: _replace(item, replacements) for key, item in value.items()}
    return value


def formal_cases():
    source = {row["id"]: row for row in source_cases()}
    output = []
    for variant, (template, names) in enumerate(zip(TEMPLATES, NAMES, strict=True), start=1):
        base = copy.deepcopy(source[template])
        old = names[0]
        new = names[1]
        replacements = [(old, new)]
        replacements.extend([
            (old.lower(), new.lower()),
            (old.lower().replace(" ", "-"), new.lower().replace(" ", "-")),
        ])
        if len(names) >= 3:
            replacements.extend((item, item.replace(old, new)) for item in names[2:] if item != old)
        for repeat in ("A", "B"):
            row = _replace(base, replacements)
            row["id"] = f"formal_{variant}_{template}_{repeat.lower()}"
            row["split"] = "formal"
            for document in row["documents"]:
                document["url"] += f"?variant={repeat.lower()}"
            row["question"] += f"\n这是正式矩阵题 {variant}{repeat}；必须保留对象、版本、条件和限制的分别对应关系。"
            output.append(row)
    return output


CASE_IDS = tuple(row["id"] for row in formal_cases())


def prepare(root: Path, base_url: str) -> None:
    v2.cases = formal_cases
    v2.DEV_IDS = CASE_IDS
    v2.prepare(root, base_url)
    contract = json.loads((root / "preregistration.json").read_text(encoding="utf8"))
    contract.update({
        "phase": "formal-v2",
        "seeds": list(SEEDS),
        "cases": list(CASE_IDS),
        "gate": "Task-level semantic net improvement on new tasks and both seeds; no serious regression; production unchanged.",
    })
    v2.write(root / "preregistration.json", contract)
    v2.write(root / "contract-hash.json", {"sha256": v2.hashlib.sha256((root / "preregistration.json").read_bytes()).hexdigest()})
    frozen = v2.json.loads((root / "frozen-inputs.json").read_text(encoding="utf8"))
    frozen.pop("contract-hash.json", None)
    frozen.pop("frozen-inputs.json", None)
    frozen["preregistration.json"] = v2.hashlib.sha256((root / "preregistration.json").read_bytes()).hexdigest()
    v2.write(root / "frozen-inputs.json", frozen)
    v2.freeze_experiment(root)


def run(root: Path) -> None:
    v2.cases = formal_cases
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
