"""Reading an execution receipt never reruns a command or accepts arbitrary paths."""
import json
from types import SimpleNamespace
from unittest import mock

import pytest

from codey.operations.task_execution import ExecutionDelegate
from codey.operations.task_session import TaskSession
from codey.runtime.core.models import ToolCall, ToolResult


@pytest.mark.parametrize("value", [None, False, 0, "", '"quoted"\\中文\u2028\u2029', {"text": "full" * 2000}])
def test_explicit_canonical_view_preserves_types_and_ignores_display(value):
    session = TaskSession(policy=SimpleNamespace(allows=lambda _: True))
    session._memory_results["exec-17"] = ToolResult(ToolCall("grep", {}), "display decoy", ok=True,
                                                   canonical={"value": value})
    delegate = ExecutionDelegate(session=session)
    result, ok, _ = delegate.execute(ToolCall("read_tool_result", {"result_ref": "exec-17", "view": "value",
                                                                              "limit": 8000}))
    assert ok
    page = json.loads(result.model_text)
    assert page["view"] == "value"
    if page["next_offset"] is None:
        assert json.dumps(json.loads(page["text"])["value"]) == json.dumps(value)
    else:
        assert page["text"].startswith('{"value":') and "display decoy" not in page["text"]


@pytest.mark.parametrize("view", ["value", "unknown", False, []])
def test_invalid_or_missing_value_view_never_falls_back_to_display(view):
    session = TaskSession(policy=SimpleNamespace(allows=lambda _: True))
    session._memory_results["exec-17"] = ToolResult(ToolCall("grep", {}), '{"value":"display decoy"}', ok=True)
    result, ok, _ = ExecutionDelegate(session=session).execute(ToolCall("read_tool_result",
        {"result_ref": "exec-17", "view": view}))
    assert not ok and "display decoy" not in result.model_text


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


def test_repeated_missing_result_reads_do_not_repeat_search_or_execution():
    session = TaskSession(policy=SimpleNamespace(allows=lambda _: True))
    delegate = ExecutionDelegate(session=session)
    with (
        mock.patch.object(delegate, "_execute_project") as project,
        mock.patch.object(delegate, "_execute_research") as research,
        mock.patch.object(delegate, "_resolve_custom_executor") as custom,
    ):
        for _ in range(2):
            result, ok, _ = delegate.execute(ToolCall("read_tool_result", {"result_ref": "missing-search"}))
            assert not ok
            assert "stored result unavailable in this task" in result.model_text
        project.assert_not_called()
        research.assert_not_called()
        custom.assert_not_called()
    assert session._memory_results == {}
