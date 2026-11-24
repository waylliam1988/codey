from pathlib import Path

import pytest

from codey.codemode.kernel import CodeModeKernelExecutor
from codey.runtime.core.models import ToolCall, ToolResult
from codey.storage.managed_outputs import ManagedOutputStore


def test_codemode_reports_an_unwritable_artifact_store(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.write_text("This path is a file.", encoding="utf-8")

    def unexpected_child(call: ToolCall) -> ToolResult:
        pytest.fail(f"unexpected child call: {call.name}")

    executor = CodeModeKernelExecutor(
        execution_id="output", catalog=(), execute_child=unexpected_child,
        artifact_store=ManagedOutputStore(state), session_id="session", run_id="run", max_inline_bytes=1,
    )
    result = executor.execute(ToolCall("codemode", {"code": "return 'result larger than one byte';"}))

    assert not result.ok
    assert "CodeMode output could not be stored" in result.model_text
    assert "value" not in result.canonical
