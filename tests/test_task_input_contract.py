"""Task submissions use explicit input variants for start, resume, and approval."""

import pytest


def test_explicit_task_inputs_are_disjoint() -> None:
    from codey.task.model import ResolveShellApproval, ResumeRun, StartTask

    assert StartTask("hello").text == "hello"
    assert ResumeRun("run-1").source_run_id == "run-1"
    assert ResolveShellApproval("approval-1").approval_id == "approval-1"


def test_approval_resume_requires_only_approval_id() -> None:
    from codey.task.model import ResolveShellApproval

    with pytest.raises(ValueError):
        ResolveShellApproval("")


def test_http_submission_rejects_implicit_continue_boolean() -> None:
    from codey.app.api import run_submit_response

    status, payload = run_submit_response(
        {"session_id": "s", "task": "continue", "continue_task": True, "provider": "local"},
        lambda *args, **kwargs: "run-1",
    )
    assert status == 400
    assert "continue_task" in str(payload["error"])


def test_http_resume_input_forwards_source_run_without_boolean() -> None:
    from unittest.mock import Mock

    from codey.app.api import run_submit_response

    submit = Mock(return_value="run-2")
    status, payload = run_submit_response(
        {"session_id": "s", "provider": "deepseek", "input": {"kind": "resume_run", "source_run_id": "run-1"}},
        submit,
    )
    assert status == 200 and payload["run_id"] == "run-2"
    assert submit.call_args.kwargs["previous_run_id"] == "run-1"
