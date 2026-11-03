"""Prepare the production follow-up under the user's revised acceptance criteria."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from tests.manual.research_framework_comparison import read_json, write_json


def prepare(root: Path, previous: Path, *, seeds: tuple[int, ...] = (121, 122),
            regression_only: bool = False) -> None:
    root.mkdir(parents=True, exist_ok=False)
    for name in ("seed-identities.json", "clock-context.txt", "material-channel.json", "fixtures.json", "limits.json"):
        shutil.copy2(previous / name, root / name)
    for name in ("warm-knowledge", "empty-knowledge", "conflict-knowledge", "revision-knowledge"):
        if any(p.stat().st_size for p in (previous / name).rglob("*-wal")):
            raise ValueError("A frozen seed has an active WAL; do not copy an incomplete database")
        shutil.copytree(previous / name, root / name, ignore=shutil.ignore_patterns("*-wal", "*-shm"))
    prior = read_json(previous / "preregistration.json")
    write_json(root / "preregistration.json", {
        "phase": "production v2: preserve original recovery context after a settled failed knowledge_read",
        "prior_phase_root": previous.as_posix(),
        "policy_change": (
            "User explicitly accepts integration for lookup benefit despite unchanged pre-existing revision failures. "
            "The phase1 requirement that both arms pass >=2/3 of every scenario is not carried forward. "
            "This is a new preregistration, not a retroactive claim that phase1 passed its original gate."
        ),
        "seeds": list(seeds),
        "matrix": [{"case": c["case"], "arms": ["full_control", "production"]} for c in prior["matrix"]
                   if not regression_only or c["case"] == "history_error"],
        "v2_change": "Successful ordinary lookup remains lightweight. Any settled failed knowledge_read restores the original full tools and revision guidance for the remaining task. Exact ids, versions, permissions and publication checks are unchanged.",
        "v1_evidence": "docs/reports/research-lazy-revision-production-v1-20261103.json retains all 24 v1 runs, including history_error seed112 regression; never replaced by v2 results.",
        "controls": [
            "Full_control restores always-visible original knowledge_commit and original revision guidance only",
            "Production uses installed routing, guidance, preparation executor, receipt and recovery without patches",
            "Same long historical guidance, operation history, schemas, fixtures, exact ids/revisions, budgets and guards",
            "Every failed run and retry included; original phase1 artifacts remain unchanged",
        ],
        "quality_rubric": prior["quality_rubric"],
        "production_gate": [
            "No baseline-passing paired task regresses on preregistered core checks",
            "No unauthorized old-object mutations, broken published evidence bindings or forbidden fetches",
            "Lookup tasks never prepare/commit or change old objects",
            "Every production edit task can reveal the identical original full commit schema",
            "Lookup aggregate input falls >=5%, lookup input+output and overall input+output fall",
            "Overall model requests do not increase; lookup input falls in both seed groups",
            "Original failed edit controls may remain failed; actual revision success is reported separately",
        ],
        "limitations": "Two new seeds on six WAL scenarios; local 12B native tool protocol. Existing semantic edit failures are accepted, not fixed or scored as successes.",
    })


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--previous", type=Path, required=True)
    parser.add_argument("--seeds", type=int, nargs="+", default=[121, 122])
    parser.add_argument("--regression-only", action="store_true")
    args = parser.parse_args()
    prepare(args.root.resolve(), args.previous.resolve(), seeds=tuple(args.seeds), regression_only=args.regression_only)
