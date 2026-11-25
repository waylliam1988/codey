from pathlib import Path

import pytest

from codey.codemode.kernel import CodeModeKernelExecutor, build_execute_turn_executor
from codey.codemode.runtime import NodeWorkerRuntime
from codey.operations.task_session import TaskSession
from codey.operations.turn_snapshot import build_turn_snapshot
from codey.policies.task_policy import TaskPolicy
from codey.runtime.core.models import ToolCall, ToolResult


@pytest.mark.parametrize("production", [False, True])
def test_kernel_classifies_unavailable_runtime_before_dispatch(tmp_path: Path, production: bool) -> None:
    def missing_runtime() -> NodeWorkerRuntime:
        return NodeWorkerRuntime(node=str(tmp_path / "missing-node.exe"))

    def unexpected_child(call: ToolCall) -> ToolResult:
        pytest.fail(f"unexpected child tool: {call.name}")

    call = ToolCall("codemode", {"code": "return 42;"})
    if production:
        session = TaskSession(TaskPolicy(frozenset({"control", "project.read"})))
        execute = build_execute_turn_executor(
            session=session, snapshot=build_turn_snapshot(session), run_id="runtime-start",
            effect_scope="test", turn=0, runtime_factory=missing_runtime,
        )
        result = execute(call)
        assert result.canonical["child_effect_ids"] == []
        assert not session.executed
    else:
        result = CodeModeKernelExecutor(
            execution_id="runtime-start", catalog=(), execute_child=unexpected_child, runtime=missing_runtime(),
        ).execute(call)
    assert not result.ok
    assert result.audit["error_code"] == "runtime_unavailable"
    assert result.audit["child_calls"] == 0
