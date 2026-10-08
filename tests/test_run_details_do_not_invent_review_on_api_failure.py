"""A rejected chat has no review status unless a review actually produced one."""
from codey.runs.details import load_run_details
from codey.runs.ledger import RunLedgerStore
from codey.runs.trace import RunTraceStore


def test_rejected_chat_without_review_does_not_show_review_incomplete(tmp_path):
    ledgers = RunLedgerStore(tmp_path)
    traces = RunTraceStore(tmp_path)
    ledger = ledgers.open(run_id="run1", session_id="session1", task="Hello", project=None, mode="chat", provider="zen")
    ledger.finish(summary="model HTTP 403", stop_reason="provider_failure", turns=1, max_turns=3, provider="zen")
    trace = traces.open(run_id="run1", session_id="session1", project=None, provider_initial="zen", mode_initial="chat")
    trace.finish(status="provider_failure", mode="chat", provider="zen")
    summary = load_run_details(run_ledgers=ledgers, run_traces=traces, session_id="session1", run_id="run1")
    assert not any(row.label == "Review" for row in summary.rows)


def test_actual_unavailable_review_is_still_displayed(tmp_path):
    traces = RunTraceStore(tmp_path)
    trace = traces.open(run_id="run1", session_id="session1", project=None, provider_initial="zen", mode_initial="review")
    trace.record_coding_review({"verdict": "unknown", "status": "unavailable", "origin": "fresh", "finding_count": 0})
    trace.finish(status="provider_failure", mode="review", provider="zen")
    summary = load_run_details(run_ledgers=RunLedgerStore(tmp_path), run_traces=traces, session_id="session1", run_id="run1")
    assert any(row.label == "Review" and row.value == "Review unavailable" for row in summary.rows)
