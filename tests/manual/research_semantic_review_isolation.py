"""Exploratory input isolation, with unchanged reviewer prompt and gold labels."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from tests.manual import research_semantic_review_ab as ab
from tests.manual.research_semantic_review_extension import fixtures

ROOT = Path("tests/manual/results/research-semantic-review-isolation-20261106")


def isolate(case, index):
    unit = ab.units(case["markdown"])[index]
    value = copy.deepcopy(case)
    value.update(id=f"{case['id']}-{unit['id']}", markdown=unit["text"], gold=[case["gold"][index]],
                 original_unit=unit)
    value["evidence"] = [row for row in value["evidence"] if row["label"] in unit["evidence_refs"]]
    return value


def main():
    selected = [isolate(case, index) for case in fixtures() if not case["id"].startswith("historical")
                for index in range(len(case["gold"]))]
    spec = {"fixtures": selected, "prompt": ab.PROMPTS["focused"], "base_source_hashes": ab.signature(),
            "isolation_hash": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "seeds": [825, 826],
            "scope": "exploratory on reused robustness cases; not heldout validation; no promotion"}
    path = ROOT / "preregistration.json"
    if path.exists():
        if json.loads(path.read_text(encoding="utf-8")) != spec:
            raise RuntimeError("isolation identity changed")
    else:
        ab.write(path, spec)
    model = ab.get("/v1/models")["data"][0]["id"]
    for case in selected:
        for seed in spec["seeds"]:
            ab.run_one(ROOT, case, seed, "focused", model)
    print("Completed isolation runs:", len(list((ROOT / "runs").glob("*.json"))))


if __name__ == "__main__":
    main()
