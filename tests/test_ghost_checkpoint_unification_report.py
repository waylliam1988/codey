"""Reject broken ownership evidence rather than awarding a checkpoint result."""
from __future__ import annotations

import json
from itertools import product
from pathlib import Path

import pytest

from tests.manual.ghost_memory_routing.checkpoint_unification_report import summarize

REPORTS = Path(__file__).parent / "manual" / "ghost_memory_routing" / "reports"


@pytest.mark.parametrize("damage", ["missing_off", "duplicate", "no_checkpoint", "no_trim", "tainted_source", "no_ack", "wrong_commit"])
@pytest.mark.parametrize("reports", [REPORTS, REPORTS / "canonical-fenced-replay-20261021"], ids=["original", "fenced"])
def test_corrupt_checkpoint_evidence_is_incomplete(damage, reports):
    matrices = {(budget, arm): json.loads((reports / f"checkpoint-{budget}-{arm}-metrics.json").read_text(encoding="utf-8"))
                for budget, arm in product(("full", "trim"), ("base", "drop", "edit"))}
    summarize(matrices)
    matrix = matrices["trim", "edit"]
    row = matrix["rows"][0]
    if damage == "missing_off":
        matrix["rows"] = [item for item in matrix["rows"] if not item["revoke"]]
    elif damage == "duplicate":
        matrix["rows"][-1] = row
    elif damage in {"no_checkpoint", "no_trim"}:
        point = next(point for point in row["checkpoints"] if point["label"] == "accepted_checkpoint_before_lifecycle")
        point["accepted_checkpoints" if damage == "no_checkpoint" else "bounded_replay_has_fact"] = 0 if damage == "no_checkpoint" else True
    elif damage == "tainted_source":
        row["canonical_edit"]["source_has_hint"] = True
    elif damage == "no_ack":
        row["middle"][0]["settled_methods"] = ["send_turn"]
    else:
        matrix["config"]["commit"] = "different-implementation"
    with pytest.raises(ValueError, match="INCOMPLETE"):
        summarize(matrices)


def test_source_fence_and_folding_preserve_the_frozen_observation_matrix():
    observations = []
    for reports in (REPORTS, REPORTS / "canonical-fenced-replay-20261021"):
        matrices = {(budget, arm): json.loads((reports / f"checkpoint-{budget}-{arm}-metrics.json").read_text(encoding="utf-8"))
                    for budget, arm in product(("full", "trim"), ("base", "drop", "edit"))}
        observations.append(summarize(matrices))
    assert observations[0] == observations[1]
