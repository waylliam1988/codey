"""Completion receipts reject truthy non-boolean verification values."""
from __future__ import annotations


def test_task_receipt_does_not_turn_string_false_checks_into_passed() -> None:
    from codey.runs.receipt import build_task_receipt

    receipt = build_task_receipt(
        {"mode": "git", "changed_count": 1}, checks_passed="false"  # type: ignore[arg-type]
    )

    assert receipt.verification.checks_passed is False


def test_run_ledger_does_not_persist_string_false_checks_as_passed(tmp_path) -> None:
    from codey.runs.ledger import RunLedgerWriter, _scan_ledger_file

    writer = RunLedgerWriter(tmp_path / "run.jsonl", run_id="r1", session_id="s1")
    writer.append_changes_collected(
        {"mode": "git", "changed_count": 1}, checks_passed="false"  # type: ignore[arg-type]
    )

    rows, complete, _seq, _truncated, _size = _scan_ledger_file(writer.path)
    assert complete
    assert rows[-1]["checks_passed"] is False
