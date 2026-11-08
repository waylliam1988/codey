"""Execution facts survive the real approval/restart receipt boundary."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from codey.operations.task_loop import run_task_kernel
from codey.runtime.core.execution_receipt import ExecutionReceipt, execution_receipt
from codey.runtime.core.models import ToolCall, ToolResult
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
        assert audit["exit_code_scope"] == "shell"
        canonical = payload["canonical"]
        assert isinstance(canonical, dict)
        execution = canonical["execution"]
        assert isinstance(execution, dict)
        assert execution["status"] == "exit" and execution["exit_code_scope"] == "shell"
        delivered = [row for kind, rows in runtime.events if kind == "results" for row in rows]
        assert len(delivered) == 1
        assert "Exit code: 7" in delivered[0].content
        assert "Capture truncated: true" in delivered[0].content
        assert "PROOF_OUTPUT" in delivered[0].content


@pytest.mark.parametrize("code", [0, 7])
def test_native_receipt_facts_and_ok_survive_durable_restart(tmp_path: Path, code: int) -> None:
    from codey.operations.task_effects import KernelEffectSink
    from codey.runtime.effects.effect_records import RuntimeEffectStore
    from codey.storage.managed_outputs import ManagedOutputStore
    from codey.toolchain.runtime import RunCommandRawResult, project_run_command_result
    from tests.support.receipt_fixtures import rebuild_settled_tool_result
    from tests.test_settled_delivery_recovery import _accept_and_mark, _new_log

    raw = RunCommandRawResult("python check.py", "proof", code == 0, code,
                              "start", "finish", 5, 5, 0, False)
    outcome = project_run_command_result(tmp_path, raw)
    result = ToolResult(ToolCall("run", {"command": raw.command}, call_id="call"), outcome.model_text,
                        ok=outcome.ok, canonical=dict(outcome.canonical), audit=dict(outcome.audit))
    expected = execution_receipt(result)
    assert expected is not None
    assert expected.facts() == ExecutionReceipt("exit", code, "proof", exit_code_scope="process").facts()
    assert result.ok is (code == 0)
    assert result.audit["exit_code_scope"] == "process"
    _, mutations, durable = _new_log(tmp_path, "s", "r")
    _accept_and_mark(mutations, "s", "r", str(tmp_path))
    store = ManagedOutputStore(tmp_path / "state")
    sink = KernelEffectSink(mutations, session_id="s", run_id="r", provider_id="local",
                            managed_outputs=store, durable=durable)
    sink.begin_turn([("effect", result.call, 0)], turn=1)
    sink.settle("effect", result.ok, result=result, exit_code=code)
    effect = RuntimeEffectStore(durable=durable).load_effects("s", "r")[0]
    restored = rebuild_settled_tool_result(effect, managed_store=store, session_id="s", run_id="r")
    assert restored is not None and restored.ok is result.ok
    assert execution_receipt(restored) == expected


def test_legacy_decoder_preserves_scope_and_does_not_infer_from_display() -> None:
    for name, scope in (("shell", "shell"), ("run", "process")):
        result = ToolResult(ToolCall(name, {}), "exit 0: misleading preview", ok=True,
                            audit={"exit_code": 7}, truncated=True)
        receipt = execution_receipt(result)
        assert receipt is not None and receipt.exit_code == 7 and receipt.exit_code_scope == scope
        assert receipt.truncated and not receipt.full_output_recoverable
    assert execution_receipt(ToolResult(ToolCall("shell", {}), "Exit code: 0", ok=True)) is None
