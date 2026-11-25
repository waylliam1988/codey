from __future__ import annotations

from pathlib import Path
from threading import Barrier

import pytest

from codey.agents import tool_execution
from codey.agents.tools import AgentToolFns, build_project_tool_fns
from codey.operations import tool_operation
from codey.operations.kernel import kernel_execution
from codey.operations.kernel.kernel_execution import execute_turn
from codey.operations.kernel_result import _error_result
from codey.operations.task_session import TaskSession
from codey.operations.turn_snapshot import build_turn_snapshot
from codey.policies.task_policy import TaskPolicy
from codey.runtime.core.models import ToolCall, ToolResult
from codey.toolchain.runtime import ToolOutcome


def test_safe_project_reads_overlap_and_settle_in_slot_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "first.txt").write_text("one", encoding="utf-8")
    (tmp_path / "second.txt").write_text("two", encoding="utf-8")
    tools = build_project_tool_fns(state_home=None)
    session = TaskSession(TaskPolicy(frozenset({"control", "project.read"})), project=str(tmp_path))
    snapshot = build_turn_snapshot(session)
    barrier = Barrier(2, timeout=4)
    execute = tool_execution.execute_information_tool_call

    def read(root: Path, tool_fns: AgentToolFns, call: ToolCall) -> ToolOutcome:
        barrier.wait()
        return execute(root, tool_fns, call)

    monkeypatch.setattr(tool_execution, "execute_information_tool_call", read)
    calls = [ToolCall("read_file", {"path": path}) for path in ("first.txt", "second.txt")]
    results = execute_turn(session, calls, project_path=tmp_path, tool_fns=tools, snapshot=snapshot)
    assert all(result.ok for result in results), [result.model_text for result in results]
    assert [result.call.args["path"] for result in results] == ["first.txt", "second.txt"]
    assert [result.call.args["path"] for result in session._memory_results.values()] == ["first.txt", "second.txt"]


def test_denied_and_failed_parallel_slots_settle_once_in_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from collections.abc import Mapping

    paths = ("denied.txt", "failed.txt", "last.txt")
    (tmp_path / paths[-1]).write_text("three", encoding="utf-8")
    tools = build_project_tool_fns(state_home=None)
    session = TaskSession(TaskPolicy(frozenset({"control", "project.read"})), project=str(tmp_path))
    guards: list[object] = []
    execute = tool_operation.execute_tool_operation

    def check(*args: object, **kwargs: object) -> ToolResult | None:
        call = args[2]
        assert isinstance(call, ToolCall)
        guards.append(call.args["path"])
        if call.args["path"] == "denied.txt":
            return _error_result(call, "fixture denial")
        return None

    def execute_read(
        session: TaskSession, call: ToolCall, *, delegate: tool_operation.ToolDelegate | None,
        runnable: Mapping[str, kernel_execution.ToolExecutor], turn: int, tool_index: int, project_path: Path | None,
        snapshot: kernel_execution.TurnSnapshot | None,
    ) -> tuple[ToolResult, bool, int | None]:
        if call.args["path"] == "failed.txt":
            raise OSError("fixture read failure")
        return execute(session, call, delegate=delegate, runnable=runnable, turn=turn,
                       tool_index=tool_index, project_path=project_path, snapshot=snapshot)

    monkeypatch.setattr(kernel_execution, "_guarded_slot_result", check)
    monkeypatch.setattr(tool_operation, "execute_tool_operation", execute_read)
    calls = [ToolCall("read_file", {"path": path}) for path in paths]
    results = execute_turn(session, calls, project_path=tmp_path, tool_fns=tools, snapshot=build_turn_snapshot(session))
    assert guards == list(paths)
    assert [result.ok for result in results] == [False, False, True]
    assert [result.call.args["path"] for result in session._memory_results.values()] == list(paths)
