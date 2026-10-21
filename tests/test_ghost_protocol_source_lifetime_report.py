"""Research reports must reject incomplete controls and broken runtime evidence."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from tests.manual.ghost_memory_routing.protocol_source_lifetime_report import summarize

REPORTS = Path(__file__).parent / "manual" / "ghost_memory_routing" / "reports"


@pytest.mark.parametrize("corruption", ["missing_off", "duplicate", "no_http", "no_ack", "lost_prior_task", "wrong_commit"])
def test_incomplete_lifetime_evidence_cannot_produce_a_report(corruption):
    baseline = json.loads((REPORTS / "protocol-source-lifetime-baseline-metrics.json").read_text(encoding="utf-8"))
    replay = json.loads((REPORTS / "protocol-source-lifetime-replay-metrics.json").read_text(encoding="utf-8"))
    # First verify that the original artifacts are a usable matched pair.
    summarize(baseline, replay)
    damaged = copy.deepcopy(replay)
    if corruption == "missing_off":
        damaged["rows"] = [row for row in damaged["rows"] if not row["revoke"]]
    elif corruption == "duplicate":
        damaged["rows"][-1] = copy.deepcopy(damaged["rows"][0])
    elif corruption == "no_http":
        damaged["rows"][0]["second"]["http"] = 0
    elif corruption == "no_ack":
        damaged["rows"][0]["second"]["settled_methods"] = ["send_turn"]
    elif corruption == "lost_prior_task":
        damaged["rows"][0]["second"]["prior_task_sent"] = False
    else:
        damaged["config"]["commit"] = "different-producer"
    with pytest.raises(ValueError, match="INCOMPLETE"):
        summarize(baseline, damaged)
