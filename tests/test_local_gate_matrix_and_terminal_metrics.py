"""Gate observations must not count duplicate attempts or receipt caps as truncation."""

import json

import pytest

from tools import local_model_gate_attempts as gate


def _row(attempt=1, case="edit"):
    return {"case": case, "attempt": attempt, "ok": True, "scope": "objective_task"}


@pytest.mark.parametrize(
    "rows",
    [
        [_row(), _row()],
        [_row(), _row(case="create")],
        [_row(), _row(attempt=2)],
        [_row(attempt=True)],
        [_row(attempt="1")],
        [_row(attempt=1.0)],
    ],
)
def test_matrix_cannot_pass_duplicate_unexpected_or_coerced_attempts(rows):
    result = gate.summarize(rows, expected_cases=("edit",), repeat=1)
    assert result["matrix_complete"] is False
    assert result["ok"] is False


def test_exact_matrix_passes():
    assert gate.summarize([_row()], expected_cases=("edit",), repeat=1)["ok"] is True


@pytest.mark.parametrize("terminal,expected", [(True, "tool_error"), (False, "truncation")])
def test_failure_classification_separates_terminal_ack_from_active_length(tmp_path, terminal, expected):
    payload = (
        {"messages": [{"role": "tool", "tool_call_id": "done-1", "content": "accepted"}], "max_tokens": 1}
        if terminal
        else {"messages": [{"role": "user", "content": "work"}], "tools": [{"type": "function"}]}
    )
    rows = [
        {"type": "request", "exchange": 1, "payload": payload},
        {"type": "response", "exchange": 1, "payload": {"choices": [{"finish_reason": "length"}]}},
    ]
    (tmp_path / "provider.jsonl").write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    metrics = gate._provider_metrics(tmp_path)
    result = gate.annotate_result(
        {"case": "edit", "ok": False, "failure_stage": "tool_order", "provider_metrics": metrics}
    )
    assert result["failure_kind"] == expected
