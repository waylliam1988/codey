"""Cold reads reject malformed artifacts and conflicting review settlements."""
import json

import pytest

from codey.reviews.core import ReviewFinding, ReviewResult
from codey.reviews.input import ReviewScope
from codey.reviews.persistence import ReviewArtifactStore, load_review_artifact, save_review_artifact
from codey.runs.ledger import RunLedgerRecord
from codey.runs.ledger_projection import project_run_ledger


@pytest.mark.parametrize("field,value", [
    ("schema_version", True), ("contract_version", True), ("attempt_id", "other"),
    ("scope", []), ("verdict", []), ("status", {}), ("verdict", "perhaps"), ("status", "whatever"), ("self_review", "false"),
])
def test_invalid_artifact_contract_is_rejected(tmp_path, field, value):
    store = ReviewArtifactStore(tmp_path)
    assert save_review_artifact(store, session_id="s", run_id="r", attempt_id="a",
        result=ReviewResult("changes_requested", "fix", [ReviewFinding("app.py", "bug")],
                            scope=ReviewScope(total_changed_files=1, provided_files=("app.py",))),
        scope_digest="s", prompt_digest="p", snapshot_digest="snap", reviewer_id="r", policy="web_if_available")
    path = store.path_for("s", "r", "a")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload[field] = value
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError):
        load_review_artifact(store, session_id="s", run_id="r", attempt_id="a")


def test_conflicting_terminal_review_events_cannot_project_clean_review():
    records = []
    for seq, (kind, verdict) in enumerate([
        ("run_started", ""), ("review_result_projected", "approved"),
        ("review_finished", "approved"), ("review_finished", "changes_requested"),
        ("run_finished", ""),
    ], 1):
        records.append(RunLedgerRecord({"schema_version": 1, "seq": seq, "type": kind,
            "run_id": "r", "session_id": "s", "review_attempt_id": "a", "status": "complete",
            "verdict": verdict, "origin": "fresh", "finding_count": 0}))
    assert project_run_ledger(records).review is None


@pytest.mark.parametrize("count", [True, "0", 0.0, -1, 9])
def test_ledger_review_count_is_exact_and_bounded(count):
    records = [RunLedgerRecord({"schema_version": 1, "seq": seq, "type": kind,
        "review_attempt_id": "a", "verdict": "approved", "status": "complete",
        "origin": "fresh", "finding_count": count})
        for seq, kind in enumerate(("review_result_projected", "review_finished"), 1)]
    assert project_run_ledger(records).review is None
