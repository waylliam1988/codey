"""Execution facts survive the real approval/restart receipt boundary."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from codey.operations.task_loop import run_task_kernel
from codey.runtime.core.execution_receipt import ExecutionReceipt
from codey.types.json_value import decode_json_object
from tests.support.kernel_restart import approved_shell


def test_status_header_reserves_budget_and_records_actual_clipping() -> None:
    receipt = ExecutionReceipt("exit", 7, "A" * 50_000, execution_id="execution_test").bounded(limit=800)
    text = receipt.render()
    assert len(text) <= 800
    assert text.startswith("Execution status: exit\nExit code: 7\nExit code scope: shell\n")
    assert "Output truncated: true" in text
    assert "Capture truncated: false" in text
    assert "Execution ID: execution_test" in text
    assert receipt.truncated


@pytest.mark.parametrize("code", [True, "7", 1.5])
def test_invalid_exit_code_is_not_coerced(code: object) -> None:
    with pytest.raises(ValueError, match="exit_code"):
        ExecutionReceipt.from_payload({"exit_code": code})


def test_unknown_exit_and_denial_do_not_become_success() -> None:
    text = ExecutionReceipt.from_payload({"status": "denied", "exit_code": None}).bounded().render()
    assert "Execution status: denied" in text
    assert "Exit code: unknown" in text
    assert "Available receipt views: output" in text


def test_recovered_receipt_keeps_execution_reference_and_capture_truncation(tmp_path: Path) -> None:
    receipt = {"ok": True, "status": "exit", "exit_code": 7, "output": "PROOF_OUTPUT",
               "truncated": True, "capture_truncated": True, "execution_id": "execution_proof"}
    with approved_shell(tmp_path, receipt) as runtime:
        result = run_task_kernel(runtime.session(), request=runtime.request)
        assert result.completed
        effect = next(row for row in runtime.durable.effects("r") if row.category == "tool_call")
        assert effect.settlement is not None
        assert effect.settlement["result_truncated"] is True
        payload = decode_json_object(json.loads(effect.settlement["result_payload"]))
        audit = payload["audit"]
        assert isinstance(audit, dict)
        assert audit["capture_truncated"] is True
        assert audit["execution_id"] == "execution_proof"
        assert audit["exit_code"] == 7
        delivered = [row for kind, rows in runtime.events if kind == "results" for row in rows]
        assert len(delivered) == 1
        assert "Exit code: 7" in delivered[0].content
        assert "Capture truncated: true" in delivered[0].content
        assert "PROOF_OUTPUT" in delivered[0].content
