"""An unknown recorded exit cannot be replaced by a success-shaped audit value."""

import pytest

from codey.operations.kernel_events import _emit_tool_results
from codey.operations.task_session import TaskSession, turn_effect_id
from codey.policies.task_policy import TaskPolicy
from codey.runtime.core.models import ToolCall, ToolResult


@pytest.mark.parametrize("exit_code", [None, True, False, "0", 0.0])
def test_recorded_unknown_exit_is_not_rescued_by_audit_zero(exit_code):
    session = TaskSession(policy=TaskPolicy(grants=frozenset({"control"})))
    session.executed[turn_effect_id("r", 1, 0)] = {"ok": False, "exit_code": exit_code}
    rows = []
    _emit_tool_results(rows.append, session,
                       [ToolResult(call=ToolCall("run", {}), ok=False, model_text="unknown", audit={"exit_code": 0})],
                       run_id="r", turn=1)
    assert len(rows) == 1
    assert rows[0].outcome.ok is False
    assert rows[0].outcome.exit_code is None
