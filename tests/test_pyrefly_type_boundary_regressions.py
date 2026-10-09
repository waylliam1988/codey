"""Regression tests for the cold-start type-boundary cleanup."""

from __future__ import annotations

from pathlib import Path

import pytest

from codey.app.event_payloads import machine_event_payload
from codey.operations.task_execution import ExecutionDelegate
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.runtime.core.models import ToolCall


def test_machine_event_payload_keeps_mixed_common_fields_as_object_values() -> None:
    payload = machine_event_payload(
        {"type": "task_start", "run_id": "run-1", "session_id": "session-1", "max_turns": 3}
    )

    assert payload == {
        "schema_version": 1,
        "type": "task_start",
        "run_id": "run-1",
        "session_id": "session-1",
        "project": "",
        "provider": "",
        "mode": "",
        "max_turns": 3,
    }


@pytest.mark.parametrize("call", [
    ToolCall("run", {"path": ".", "command": "python -V"}),
    ToolCall("read_file", {"path": "a.py"}),
    ToolCall("edit", {"path": "a.py", "content": "x = 1\n"}),
])
def test_execution_delegate_fails_closed_when_project_tools_are_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, call: ToolCall,
) -> None:
    from codey.agents import tools

    monkeypatch.setattr(tools, "DEFAULT_TOOL_FNS", None)
    session = TaskSession(policy=TaskPolicy(grants=frozenset({"project.read", "project.verify", "project.write"})))
    delegate = ExecutionDelegate(session=session, project_path=tmp_path, tool_fns=None)
    assert delegate.tool_fns is None

    result, ok, exit_code = delegate.execute(call)

    assert ok is False
    assert exit_code is None
    assert result.ok is False
    assert result.model_text == "ERROR: project tools unavailable"
    assert not (tmp_path / "a.py").exists()


def test_execution_delegate_fails_closed_without_task_session(tmp_path: Path) -> None:
    result, ok, exit_code = ExecutionDelegate(project_path=tmp_path).execute(
        ToolCall("run", {"path": ".", "command": "python -V"})
    )
    assert ok is False
    assert exit_code is None
    assert result.model_text == "ERROR: task session unavailable"


def test_execution_delegate_edit_fails_closed_when_tools_disappear(tmp_path: Path) -> None:
    delegate = ExecutionDelegate(project_path=tmp_path)
    delegate.tool_fns = None
    outcome = delegate._execute_edit(ToolCall("edit", {"path": "a.py", "content": "x = 1\n"}))
    assert outcome.ok is False
    assert outcome.model_text == "ERROR: project tools unavailable"
    assert not (tmp_path / "a.py").exists()
