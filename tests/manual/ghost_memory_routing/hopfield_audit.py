"""Post-hoc descriptive audit; does not alter frozen scores or make model requests."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import cast

from tests.manual.ghost_memory_routing.hopfield_experiment import ROOT, check_freeze, dump
from tests.manual.ghost_memory_routing.hopfield_protocol import ARMS


def audit(home: Path, target: Path) -> None:
    check_freeze(home)
    rows: list[dict[str, object]] = [json.loads(line) for line in
                                   (home / "live/cells.jsonl").read_text(encoding="utf-8").splitlines()]
    paired = {(str(row["query"]), str(row["arm"])): row for row in rows}
    wins: list[dict[str, object]] = []
    for row in rows:
        if row["arm"] != "hopfield":
            continue
        baseline = paired[str(row["query"]), "cosine"]
        if row["behavior_correct"] and not baseline["behavior_correct"]:
            wins.append({
                "query": row["query"], "family": row["family"],
                "hopfield_selected": row["selected"], "cosine_selected": baseline["selected"],
                "hopfield_relevant_hit": row["hit_at_2"], "cosine_relevant_hit": baseline["hit_at_2"],
            })
    before_path = home.parent / "production-before.json"
    before = json.loads(before_path.read_text(encoding="utf-8")) if before_path.exists() else None
    after = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in (ROOT / "codey").rglob("*.py")}
    dump(target, {
        "kind": "POST_HOC_DESCRIPTIVE_AUDIT_NOT_FROZEN_PRIMARY_SCORER",
        "complete_cells": len(rows), "source_freeze_verified": True,
        "production_files": len(after), "production_hashes_unchanged": before == after if before is not None else None,
        "physical_request_ids": len({row["physical_request"] for row in rows}),
        "hopfield_behavior_wins_over_cosine": wins,
        "correct_without_relevant_retrieval": {
            arm: sum(bool(row["behavior_correct"]) and row["hit_at_2"] is False
                     for row in rows if row["arm"] == arm) for arm in ARMS
        },
        "behavior_correct_with_relevant_retrieval_or_correct_abstention": {
            arm: sum(bool(row["behavior_correct"])
                     and (bool(row["hit_at_2"]) or (row["expected"] == "ask_user" and not row["selected"]))
                     for row in rows if row["arm"] == arm) for arm in ARMS
        },
        "nonempty_irrelevant_context_on_lifecycle_queries": {
            arm: sum(bool(row["selected"]) for row in rows if row["arm"] == arm and row["family"] == "lifecycle")
            for arm in ARMS
        },
        "exposure_total": sum(int(cast(int, row["ineligible_exposures"])) for row in rows),
        "meaning": "Correct action labels without the relevant experience are not evidence of successful recall.",
    })


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="original frozen run directory")
    parser.add_argument("--report", type=Path, required=True)
    options = parser.parse_args()
    audit(options.output, options.report)


if __name__ == "__main__":
    main()
