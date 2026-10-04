"""Cold HTTP reads and repeated reuse consume the same verified artifact."""
from dataclasses import replace
from types import SimpleNamespace

from codey.reviews.core import ReviewFinding, ReviewResult
from codey.reviews.identity import ReviewIdentity
from codey.reviews.input import ReviewScope
from codey.reviews.persistence import ReviewArtifactStore, append_review_result_ledger, save_review_artifact
from codey.reviews.reuse import try_reuse_review
from codey.runs.ledger import RunLedgerStore


def source(tmp_path, stop_reason="done"):
    scope = ReviewScope(total_changed_files=1, provided_files=("app.py",))
    ref = save_review_artifact(ReviewArtifactStore(tmp_path), session_id="s", run_id="r1", attempt_id="a1",
        result=ReviewResult("changes_requested", "fix", [ReviewFinding("app.py", "bug")], scope=scope),
        scope_digest="scope", prompt_digest="prompt", snapshot_digest="snap", reviewer_id="local",
        model_id="known", policy="web_if_available")
    assert ref is not None
    identity = ReviewIdentity("scope", "prompt", "snap", "p", "local", "known", 1, "web_if_available", False,
                              attempt_id="a1", artifact_sha256=ref.sha256)
    ledger = RunLedgerStore(tmp_path).open(run_id="r1", session_id="s", project="p", task="review", provider="local", mode="review")
    append_review_result_ledger(lambda action: action(ledger), ReviewResult(
        "changes_requested", "fix", [ReviewFinding("app.py", "bug")], scope=scope, identity=identity))
    ledger.finish(summary="done", stop_reason=stop_reason, turns=1, max_turns=1, provider="local")
    return scope, identity


def test_cold_http_read_restores_findings_from_formal_store(tmp_path):
    from codey.app import api

    source(tmp_path)
    reader = getattr(api, "run_review_response", None)
    assert callable(reader), "structured review has no HTTP consumer"
    status, payload = reader(SimpleNamespace(state_home=tmp_path), {"session_id": ["s"], "run_id": ["r1"]})
    assert status == 200
    assert payload["review"]["findings"][0]["issue"] == "bug"


def test_reusing_a_reused_result_keeps_original_artifact_lineage(tmp_path):
    scope, identity = source(tmp_path)
    reused = try_reuse_review(state_home=tmp_path, session_id="s", current_run_id="r2", current_project="p",
        source_run_id="r1", current_scope=scope, current_identity=identity, current_snapshot_ok=True)
    assert reused is not None
    ledger = RunLedgerStore(tmp_path).open(run_id="r2", session_id="s", project="p", task="review", provider="local", mode="review")
    append_review_result_ledger(lambda action: action(ledger), reused)
    ledger.finish(summary="done", stop_reason="done", turns=1, max_turns=1, provider="local")
    twice = try_reuse_review(state_home=tmp_path, session_id="s", current_run_id="r3", current_project="p",
        source_run_id="r2", current_scope=scope, current_identity=replace(identity), current_snapshot_ok=True)
    assert twice is not None
    assert twice.source_run_id == "r1"
    assert twice.findings == reused.findings


def test_failed_source_run_cannot_be_reused(tmp_path):
    scope, identity = source(tmp_path, stop_reason="provider_failure")
    assert try_reuse_review(state_home=tmp_path, session_id="s", current_run_id="r2", current_project="p",
        source_run_id="r1", current_scope=scope, current_identity=identity, current_snapshot_ok=True) is None


def test_terminal_projection_includes_review_even_without_verification_receipt(tmp_path):
    from codey.runs.ledger_projection import event_with_projected_receipt

    source(tmp_path)
    event = {"type": "task_done", "run_id": "r1", "session_id": "s"}
    projected = event_with_projected_receipt(RunLedgerStore(tmp_path), event, session_id="s", run_id="r1")
    assert projected["review"]["finding_count"] == 1
    assert projected["review"]["status"] == "complete"
    assert "review" not in event
