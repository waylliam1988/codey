"""The sole driver reconciles original approval receipts after SQLite restart."""
import pytest

from codey.operations.task_loop import run_task_kernel
from tests.support.kernel_restart import approved_shell


@pytest.mark.parametrize("ok,exit_code,status", [(True, 0, "exit"), (False, 7, "exit"), (False, None, "denied")])
def test_receipt_recovery_survives_restart_and_is_idempotent(tmp_path, ok, exit_code, status):
    receipt = {"ok": ok, "exit_code": exit_code, "status": status,
               "output": "result" if ok else "failed", "truncated": False}
    with approved_shell(tmp_path, receipt) as runtime:
        result = run_task_kernel(runtime.session(), request=runtime.request)
        assert result.completed, result
        tool = next(effect for effect in runtime.durable.effects("r") if effect.category == "tool_call")
        assert tool.intent["call_id"] == "original-shell"
        assert tool.settlement["status"] == ("ok" if ok else "error")
        assert tool.settlement.get("exit_code") == exit_code
        assert tool.settlement.get("replay_count", 0) == 0
        delivered = [row for kind, rows in runtime.events if kind == "results" for row in rows]
        assert len(delivered) == 1 and delivered[0].call_id == "original-shell"
        assert receipt["output"] in delivered[0].content
        before = (runtime.durable.task_facts("r"), list(runtime.events))
        assert run_task_kernel(runtime.session(), request=runtime.request) == result
        assert (runtime.durable.task_facts("r"), runtime.events) == before


@pytest.mark.parametrize("corruption", ["call_id", "tool_args"])
def test_mismatched_approval_never_settles_original_call_as_success(tmp_path, corruption):
    def corrupt(command):
        command[corruption] = "another-call" if corruption == "call_id" else {"command": "echo changed", "path": "."}
    with approved_shell(tmp_path, {"ok": True}, command_change=corrupt) as runtime:
        result = run_task_kernel(runtime.session(), request=runtime.request)
        assert result.stop_reason == "recovery_failure", result
        tool = next(effect for effect in runtime.durable.effects("r") if effect.category == "tool_call")
        assert tool.settlement is None or tool.settlement["status"] != "ok"
        assert not any(kind == "results" for kind, _ in runtime.events)


def test_missing_approval_receipt_preserves_original_pending_effect(tmp_path):
    with approved_shell(tmp_path, {"ok": True, "exit_code": 0, "status": "exit", "output": "hi"}) as runtime:
        record = runtime.durable.approval("approval")
        runtime.outputs._handle_path("s", "r", record.settlement["result_ref"], ".json").unlink()
        result = run_task_kernel(runtime.session(), request=runtime.request)
        assert not result.completed
        tool = next(effect for effect in runtime.durable.effects("r") if effect.category == "tool_call")
        assert tool.settlement is None
        assert not any(kind == "results" for kind, _ in runtime.events)
