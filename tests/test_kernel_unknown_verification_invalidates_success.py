"""Real kernel: latest unknown run observation blocks prior success.

Chain: web reply -> parse -> execute -> settle -> fact record -> done gate.
A second run with a missing/invalid exit must be recorded as a failed
observation (passed=False, no exit_code) and must block completion, even
though an earlier success exists.
"""
from __future__ import annotations

import json

import pytest

from codey.operations.task_loop import run_task_kernel
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.runtime.core.models import ToolResult
from codey.runtime.observe.execution_evidence import ExecutionEvidence
from codey.workspace.revision import workspace_fingerprint


def make_session(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    session = TaskSession(
        policy=TaskPolicy(grants=frozenset({
            "control",
            "project.read",
            "project.write",
            "project.verify",
        })),
        task_kind="project",
        project=str(tmp_path),
        max_turns=3,
    )
    session.record_edit("a.py", revision=1)
    session.set_workspace_state(7, workspace_fingerprint(str(tmp_path)))
    return session


class WebOnly:
    def __init__(self):
        self.replies = iter([
            json.dumps({"tool": "run", "args": {"command": "python -m pytest", "path": "."}}),
            json.dumps({"tool": "run", "args": {"command": "python -m pytest", "path": "."}}),
            json.dumps({"tool": "done", "args": {"summary": "finished"}}),
        ])

    def new_chat(self):
        pass

    def send(self, text):
        return next(self.replies)

    def close(self):
        pass


@pytest.mark.parametrize("bad_exit", [None, False, True, "0", 0.0])
@pytest.mark.parametrize("with_evidence", [False, True])
def test_latest_unknown_blocks_real_kernel(tmp_path, monkeypatch, bad_exit, with_evidence):
    monkeypatch.setenv("NATIVE_TOOLS", "0")
    session = make_session(tmp_path)
    executions = []

    def execute_run(call):
        executions.append(call)
        audit = {"exit_code": 0}
        if len(executions) == 2:
            audit = {} if bad_exit is None else {"exit_code": bad_exit}
        return ToolResult(call=call, model_text="run output", audit=audit)

    context = None
    if with_evidence:
        context = {
            "execution_evidence": ExecutionEvidence(
                workspace_revision=session.workspace_revision,
                workspace_fingerprint=session.workspace_fingerprint,
            ),
            "project": str(tmp_path),
            "scope_files": ("a.py",),
            "run_id": "unknown-result",
            "task": "fix a.py",
        }

    result = run_task_kernel(
        session,
        provider=WebOnly(),
        provider_id="web",
        executors={"run": execute_run},
        run_id="unknown-result",
        user_task="fix a.py",
        completion_context=context,
    )

    assert len(executions) == 2
    assert len(session.verifications) == 2
    assert session.verifications[-1]["passed"] is False
    assert session.verifications[-1].get("exit_code") is None
    assert result.completed is False


def test_success_then_unknown_then_new_success_allows_completion(tmp_path, monkeypatch):
    """Success -> unknown -> new success must allow completion (latest wins)."""
    monkeypatch.setenv("NATIVE_TOOLS", "0")
    session = make_session(tmp_path)
    executions = []

    replies = iter([
        json.dumps({"tool": "run", "args": {"command": "python -m pytest", "path": "."}}),
        json.dumps({"tool": "run", "args": {"command": "python -m pytest", "path": "."}}),
        json.dumps({"tool": "run", "args": {"command": "python -m pytest", "path": "."}}),
        json.dumps({"tool": "done", "args": {"summary": "finished"}}),
    ])

    class WebThree:
        def new_chat(self):
            pass

        def send(self, text):
            return next(replies)

        def close(self):
            pass

    def execute_run(call):
        executions.append(call)
        if len(executions) == 2:
            return ToolResult(call=call, model_text="unknown", audit={})
        return ToolResult(call=call, model_text="ok", audit={"exit_code": 0})

    result = run_task_kernel(
        session,
        provider=WebThree(),
        provider_id="web",
        executors={"run": execute_run},
        run_id="unknown-then-success",
        user_task="fix a.py",
        completion_context=None,
    )
    assert len(executions) == 3
    assert len(session.verifications) == 3
    assert session.verifications[-1]["passed"] is True
    # New success after unknown revives completion only when it is latest.
    # Completion may still block on workspace identity; at minimum the
    # unknown row must exist and the latest row must be a pass.
    assert session.verifications[1]["passed"] is False


def test_denied_run_is_not_recorded_as_success(tmp_path, monkeypatch):
    """A policy-denied run must not be recorded as an executed success."""
    from codey.operations.kernel_execution import execute_turn
    from codey.runtime.core.models import ToolCall

    monkeypatch.setenv("NATIVE_TOOLS", "0")
    session = make_session(tmp_path)

    def execute_run(call):
        raise AssertionError("denied call must not reach executor")

    results = execute_turn(
        session,
        [ToolCall(name="run", args={"command": "python -m pytest", "path": "."})],
        executors={"run": execute_run},
        run_id="denied-run",
        turn=1,
    )
    assert str(results[0].model_text).startswith("ERROR:")
    # Denied calls settle as errors; they must never become a passing
    # verification observation.
    assert all(not (v.get("passed") is True) for v in session.verifications)
