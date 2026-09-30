"""Structured execution fields never cross projections via truthiness."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace


def test_project_edit_projection_rejects_string_false_ok() -> None:
    from codey.operations.project_completion_context import handle_project_tool_event

    updates: list[object] = []
    event = SimpleNamespace(
        call=SimpleNamespace(name="edit", args={"path": "a.py"}),
        outcome=SimpleNamespace(ok="false", changed=True),
        turn=1,
        metadata={"tool_index": 0},
    )
    work = SimpleNamespace(workspace_revision=1, workspace_fingerprint="")
    deps = SimpleNamespace(persistence=SimpleNamespace(project_facts=None))

    handle_project_tool_event(
        deps,
        event=event,
        project=".",
        work=work,
        run_id="r1",
        update_checkpoint=lambda action: updates.append(action),
    )

    assert updates == []


def test_workspace_edit_event_rejects_string_false_ok() -> None:
    from codey.operations.task_phases.hooks import _workspace_edit_event

    event = SimpleNamespace(
        kind="tool",
        call=SimpleNamespace(name="edit"),
        outcome=SimpleNamespace(ok="false", changed=True),
    )

    assert _workspace_edit_event(event) is False


def test_project_delegate_rejects_string_false_outcome_ok() -> None:
    from codey.operations.task_execution import ExecutionDelegate
    from codey.runtime.core.models import ToolCall

    class ToolFns:
        def execute_run_command(self, *_args, **_kwargs):
            return SimpleNamespace(
                model_text="command failed",
                ok="false",
                exit_code=1,
                audit={},
                presentation={},
                canonical={},
                truncated=False,
            )

    delegate = ExecutionDelegate(project_path=Path("."), tool_fns=ToolFns())
    delegate._policy_check = lambda _call: (False, "", False)
    result, ok, _opened, _evidence, exit_code = delegate._execute_project(
        ToolCall(name="run", args={"command": "false", "path": "."})
    )

    assert result.model_text == "command failed"
    assert ok is False
    assert exit_code == 1


def test_research_outcome_rejects_string_false_ok() -> None:
    # 生产严格性门：恢复行 ok 必须为严格布尔，非布尔直接失败关闭。
    from types import SimpleNamespace

    from codey.operations.kernel_errors import RecoveryFailed
    from codey.operations.kernel_recovery_result import frame_outcome_ok

    with __import__("pytest").raises(RecoveryFailed):
        frame_outcome_ok(SimpleNamespace(outcome=SimpleNamespace(ok="false")))


def test_explicit_edit_string_false_changed_fails_closed_without_workspace_bump(tmp_path) -> None:
    from codey.operations.kernel_execution import execute_turn
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.runtime.core.models import ToolCall, ToolResult

    class Store:
        def __init__(self) -> None:
            self.bump_calls = 0

        def bump_state(self, *_args, **_kwargs):
            self.bump_calls += 1
            raise AssertionError("invalid changed flag must not bump workspace")

    session = TaskSession(
        policy=TaskPolicy(grants=frozenset({"project.write", "control"})),
        task_kind="project",
        project=str(tmp_path),
    )
    store = Store()

    def executor(call: ToolCall) -> ToolResult:
        return ToolResult(call=call, model_text="edited", audit={"changed": "false"})

    results = execute_turn(
        session,
        [ToolCall(name="edit", args={"path": "a.py", "content": "x"})],
        executors={"edit": executor},
        run_id="r-invalid-changed",
        effect_scope="task",
        turn=1,
        project_path=tmp_path,
        workspace_revision_store=store,
    )

    assert results[0].model_text.startswith("ERROR:")
    assert store.bump_calls == 0
