"""Consumers retain findings; post-send failures never route a second request."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from codey.app.review_service import run_review, run_review_attempt
from codey.operations.review_flow import render_review_only_summary
from codey.reviews.coordinator import _snapshot_still_current
from codey.reviews.core import ReviewFinding, ReviewResult
from codey.reviews.identity import build_identity, capture_snapshot
from codey.reviews.input import prepare_review_input
from codey.runs.details import _review_summary
from codey.runtime.core.cancellation import DeadlineExceeded


def test_partial_review_only_keeps_actionable_issue_text():
    text = render_review_only_summary(ReviewResult("changes_requested", "partial", [ReviewFinding("app.py", "real bug")], status="incomplete"))
    assert "real bug" in text
    assert "Partial" in text


def test_run_details_does_not_mix_trace_count_with_ledger_result():
    review = SimpleNamespace(verdict="changes_requested", status="complete", origin="fresh", finding_count=2)
    assert _review_summary(SimpleNamespace(review=review), {"coding_review": {"finding_count": 99}})[0] == "2 issues found"


def test_run_details_hides_reuse_run_id():
    review = SimpleNamespace(verdict="approved", status="complete", origin="reused", finding_count=0, source_run_id="internal-run")
    assert "internal-run" not in _review_summary(SimpleNamespace(review=review), {})[0]


def test_post_send_failure_does_not_try_another_reviewer(monkeypatch):
    connected = Mock(return_value=object())
    monkeypatch.setattr("codey.app.review_service.providers.reviewer_candidates", lambda *_: ("first", "second"))
    monkeypatch.setattr("codey.app.review_service.providers.connect_existing_provider", connected)
    monkeypatch.setattr("codey.app.review_service.run_review_attempt", Mock(side_effect=RuntimeError("post-send failure")))
    with pytest.raises(RuntimeError, match="post-send failure"):
        run_review(SimpleNamespace(set_provider_session=lambda *_: None), session_id="s", project="p", task="review",
                   writer_summary="", changes={}, recent_log="", writer_id="writer", review_impact_map="")
    assert connected.call_count == 1


def test_review_deadline_preserves_lifecycle_signal(tmp_path):
    (tmp_path / "app.py").write_text("x", encoding="utf-8")
    reviewer = SimpleNamespace(new_chat=lambda: None, close=lambda: None,
                               send=Mock(side_effect=DeadlineExceeded("deadline")))
    with pytest.raises(DeadlineExceeded):
        run_review_attempt(SimpleNamespace(state_home=None, emit=lambda _: None), session_id="s", project=str(tmp_path),
            task="review", writer_summary="", changes={"ok": True, "files": [{"path": "app.py"}], "diff": ""},
            recent_log="", change_brief="", project_map="", verification_map="", review_impact_map="",
            execution_evidence="", reviewer_id="local", reviewer=reviewer, self_review=False)
    assert reviewer.send.call_count == 1


def test_review_for_another_workspace_cannot_drive_repair(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "app.py").write_text("x", encoding="utf-8")
    prepared = prepare_review_input(project=str(first), task="review", writer_summary="", changes={"ok": True, "files": [{"path": "app.py"}], "diff": ""})
    identity = build_identity(prepared, reviewer_id="local", policy="web_if_available", project=str(first), snapshot=capture_snapshot(first, ("app.py",)))
    assert not _snapshot_still_current(second, ReviewResult("changes_requested", "fix", [ReviewFinding("app.py", "bug")], identity=identity))


@pytest.mark.parametrize("identity", [None, SimpleNamespace(snapshot_root="", snapshot_files=())])
def test_missing_snapshot_identity_cannot_authorize_repair(identity):
    review = ReviewResult("changes_requested", "fix", [ReviewFinding("app.py", "bug")], identity=identity)
    assert not _snapshot_still_current("project", review)
