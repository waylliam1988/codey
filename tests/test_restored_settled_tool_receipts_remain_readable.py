"""Restarted tasks retain scoped result bodies, not just completion facts."""
from codey.operations.task_execution import ExecutionDelegate
from codey.runtime.core.models import ToolCall
from codey.storage.managed_outputs import ManagedOutputStore
from tests.test_session_log_receipt_recovery_preserves_facts import _dirs, _recover_formal, _settle_edit_then_runs


def test_formal_restart_can_read_the_existing_settled_result_without_execution(tmp_path):
    project, state, logs = _dirs(tmp_path)
    _, _, counts, _ = _settle_edit_then_runs(project, state, logs, "s", "r", [{"exit_code": 0}])
    session, recovery, _, _, _ = _recover_formal(project, state, logs, "s", "r")
    run_row = next(row for row in recovery.settled_tool_outcomes if row.call.name == "run")
    delegate = ExecutionDelegate(session=session, managed_outputs=ManagedOutputStore(state), session_id="s", run_id="r")
    result, ok, _ = delegate.execute(ToolCall("read_tool_result", {"result_ref": run_row.effect_id}))
    assert ok and "run out" in result.model_text
    assert counts == {"edit": 1, "run": 1}
