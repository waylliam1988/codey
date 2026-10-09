"""Reading an execution receipt never reruns a command or accepts arbitrary paths."""
from types import SimpleNamespace

from codey.operations.task_execution import ExecutionDelegate
from codey.operations.task_session import TaskSession
from codey.runtime.core.models import ToolCall, ToolResult


def test_read_tool_result_is_scoped_paginated_and_has_no_executor_side_effects():
    session = TaskSession(policy=SimpleNamespace(allows=lambda _: True))
    session._memory_results["exec-17"] = ToolResult(ToolCall("run", {"command": "python -m pytest -q"}),
                                                    "FAILED\nexact result\n", ok=False, audit={"exit_code": 1})
    delegate = ExecutionDelegate(session=session)
    assert delegate.handles("read_tool_result")
    result, ok, _ = delegate.execute(ToolCall("read_tool_result", {"result_ref": "exec-17", "offset": 7, "limit": 5}))
    assert ok and "exact" in result.model_text
    assert "exit_code" in result.model_text and "1" in result.model_text
    assert len(session._memory_results) == 1
    missing, ok, _ = delegate.execute(ToolCall("read_tool_result", {"result_ref": "../other/session"}))
    assert not ok and "unavailable" in missing.model_text


def test_modified_managed_output_does_not_return_unverified_bytes(tmp_path):
    from codey.storage.managed_outputs import ManagedOutputStore

    store = ManagedOutputStore(tmp_path)
    ref = store.write_tool_output(session_id="s", run_id="r", tool_id="exec-17", permission_profile="coding_writer",
                                  tool_name="run", display_ref="pytest", text="long original result")
    assert ref
    session = TaskSession(policy=SimpleNamespace(allows=lambda _: True))
    session._memory_results["exec-17"] = ToolResult(ToolCall("run", {}), "excerpt", ok=False,
        audit={"managed_output": {"handle": ref.handle, "sha256": ref.sha256, "original_sha256": ref.original_sha256,
                                 "original_bytes": ref.original_bytes, "stored_bytes": ref.stored_bytes, "stored_truncated": False}})
    ref.path.write_text("tampered", encoding="utf-8")
    delegate = ExecutionDelegate(session=session, managed_outputs=store, session_id="s", run_id="r")
    result, ok, _ = delegate.execute(ToolCall("read_tool_result", {"result_ref": "exec-17"}))
    assert not ok and "tampered" not in result.model_text
