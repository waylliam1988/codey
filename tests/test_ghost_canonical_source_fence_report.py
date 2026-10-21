"""Transaction races cannot be reported as behavior samples or missing controls."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from tests.manual.ghost_memory_routing.canonical_source_fence_report import summarize

REPORT = Path(__file__).parent / "manual/ghost_memory_routing/reports/canonical-source-fence-metrics.json"


@pytest.mark.parametrize("damage", ["missing_arm", "duplicate", "no_auxiliary", "no_settlement", "no_fact",
                                  "hint_contamination", "invented_final", "invented_final_score", "no_commit"])
def test_incomplete_source_transaction_evidence_is_rejected(damage):
    original = json.loads(REPORT.read_text(encoding="utf-8"))
    assert summarize(original)["cells"] == 42
    broken = copy.deepcopy(original)
    if damage == "missing_arm":
        broken["rows"] = [row for row in broken["rows"] if row["arm"] != "revision"]
    elif damage == "duplicate":
        broken["rows"][-1] = copy.deepcopy(broken["rows"][0])
    elif damage == "no_auxiliary":
        broken["rows"][0]["auxiliary_http"] = 0
    elif damage == "no_settlement":
        broken["rows"][0]["prelude_settlements"] = 9
    elif damage == "no_fact":
        broken["rows"][0]["canonical_source_has_fact"] = False
    elif damage == "hint_contamination":
        broken["rows"][0]["canonical_source_has_hint"] = True
    elif damage == "invented_final":
        broken["rows"][0]["final_task_ran"] = True
    elif damage == "invented_final_score":
        broken["rows"][0]["final_hint_result"] = False
    else:
        broken["config"]["commit"] = ""
    with pytest.raises(ValueError, match="INCOMPLETE"):
        summarize(broken)
