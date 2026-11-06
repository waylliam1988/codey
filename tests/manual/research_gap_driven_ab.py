"""Isolated A/B for gap driven next action guidance.

The candidate only appends one short instruction to the existing model facing
Research guidance.  It does not add a planner, tool, reviewer, or publication
path.  Use the existing frozen Research worker and score the resulting runs
with an independent rubric.
"""

from __future__ import annotations

import argparse
import json
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from tests.manual import research_branch_review as branch

ARMS = ("baseline", "gap_directive")
GAP_DIRECTIVE = (
    "Choose the next search or read to resolve one specific uncertainty that could change the answer to the "
    "original task. When a conclusion depends on a disputed or conditional premise, look for the relevant "
    "primary-source limitation or counterexample before spending budget on more general supporting summaries."
)


@contextmanager
def treatment(arm: str):
    """Patch only the model-facing guidance inside an isolated worker."""

    if arm not in ARMS:
        raise ValueError(arm)
    if arm == "baseline":
        yield
        return
    from codey.research import answer_context, answer_contract

    guidance = answer_contract.ANSWER_GUIDANCE + "\n" + GAP_DIRECTIVE
    with patch.object(answer_contract, "ANSWER_GUIDANCE", guidance), patch.object(
        answer_context, "ANSWER_GUIDANCE", guidance
    ):
        yield


def worker(root: Path, directory: Path, base_url: str, case: str, seed: int, arm: str) -> None:
    """Run the normal isolated worker with the treatment selected in-process."""

    with patch.object(branch, "ablation", treatment):
        branch.worker(root, directory, base_url, case, seed, arm)


def validate_preregistration(payload: object) -> None:
    if not isinstance(payload, dict):
        raise ValueError("preregistration must be an object")
    if tuple(payload.get("arms", ())) != ARMS:
        raise ValueError("preregistration arms must be baseline,gap_directive")
    if payload.get("treatment") != GAP_DIRECTIVE:
        raise ValueError("candidate treatment does not match the frozen contract")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("worker", "check"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:5001")
    parser.add_argument("--case")
    parser.add_argument("--seed", type=int)
    parser.add_argument("--arm", choices=ARMS)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.action == "check":
        validate_preregistration(json.loads((root / "preregistration.json").read_text(encoding="utf-8")))
        print("OK", root)
        return
    if args.directory is None or args.case is None or args.seed is None or args.arm is None:
        raise SystemExit("worker requires --directory, --case, --seed and --arm")
    worker(root, args.directory.resolve(), args.base_url, args.case, args.seed, args.arm)


if __name__ == "__main__":
    main()
