"""Approval receipts settle the original unsafe call without executing it again."""

from types import SimpleNamespace

import pytest

from codey.durable import DurableSession, SQLiteDurableStore
from codey.operations.recovery import recover_effects_for_resume
from codey.operations.task_effects import KernelEffectSink
from codey.runtime.core.models import ToolCall
from codey.runtime.effects.effect_records import RuntimeEffectStore
from codey.runtime.write.mutation_line import RuntimeMutationLine


def _pending_shell(tmp_path):
    durable = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    durable.admit_input(conversation_id="s", request_id="request", task_id="r", text="shell",
                        provider_snapshot={"provider_id": "local"})
    durable.start_task("r", state={})
    durable.advance_task(task_id="r", phase="assistant_response_pending", state={})
    line = RuntimeMutationLine(durable=durable)
    line.accept_operation(session_id="s", run_id="r", provider_id="local", turn_budget=6,
                          max_repair_rounds=1, task_kind="chat")
    line.mark_writer_running("s", "r", provider_id="local")
    sink = KernelEffectSink(line, session_id="s", run_id="r", provider_id="local", durable=durable)
    call = ToolCall("shell", {"command": "echo result", "path": "."}, call_id="original-call")
    sink.begin_turn([("original-effect", call, 0)], turn=1)
    durable.advance_task(task_id="r", phase="tool_effect_running", state={
        "resume_input": {"kind": "resolve_shell_approval", "approval_id": "approval"},
    })
    return durable


def _deps(durable, payload):
    return SimpleNamespace(
        state=SimpleNamespace(durable=durable, resolve_shell_approval=lambda _approval: payload),
        runtime_mutations=RuntimeMutationLine(durable=durable),
        runtime_effects=RuntimeEffectStore(durable=durable), managed_outputs=None,
    )


@pytest.mark.parametrize("ok,exit_code,status", [(True, 0, "exit"), (False, 7, "exit"), (False, None, "denied")])
def test_receipt_recovery_survives_restart_and_is_idempotent(tmp_path, monkeypatch, ok, exit_code, status):
    durable = _pending_shell(tmp_path)
    durable.close()
    payload = {"call_id": "original-call", "command": "echo result", "cwd": ".",
               "ok": ok, "exit_code": exit_code, "model_text": "result" if ok else "failed",
               "error_code": status, "truncated": False}

    def forbidden(*_args, **_kwargs):
        raise AssertionError("approval recovery must never execute a tool")

    monkeypatch.setattr("codey.operations.recovery.execute_information_tool_call", forbidden)
    for _ in range(2):
        durable = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
        try:
            before_entries = durable.entries("s")
            recovered = recover_effects_for_resume(
                _deps(durable, payload), session_id="s", run_id="r", project="", task_kind="chat",
            )
            assert recovered.ok
            assert recovered.recovered_tool_outcomes == ()
            assert len(recovered.settled_tool_outcomes) == 1
            row = recovered.settled_tool_outcomes[0]
            assert row.call.call_id == "original-call"
            assert row.outcome.ok is ok
            assert row.outcome.exit_code == exit_code
            receipt = durable.effect("original-effect").settlement
            assert receipt.get("replay_count", 0) == 0
            assert receipt["status"] == ("ok" if ok else "error")
            assert durable.task("r").phase == "tool_outcome_ready"
            assert durable.entries("s") == before_entries
        finally:
            durable.close()


@pytest.mark.parametrize("corruption", ["missing", "call_id", "command"])
def test_unverified_or_mismatched_approval_never_settles_original_call(tmp_path, corruption):
    durable = _pending_shell(tmp_path)
    payload = {"call_id": "original-call", "command": "echo result", "cwd": ".",
               "ok": True, "exit_code": 0, "model_text": "result", "error_code": "exit", "truncated": False}
    if corruption == "missing":
        payload = None
    else:
        payload[corruption] = "different"
    try:
        with pytest.raises(ValueError, match="shell approval"):
            recover_effects_for_resume(
                _deps(durable, payload), session_id="s", run_id="r", project="", task_kind="chat",
            )
        assert durable.effect("original-effect").settlement is None
    finally:
        durable.close()
