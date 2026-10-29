"""Typed approval resumes deliver durable results to browser text providers."""
from types import SimpleNamespace

import pytest

from codey.operations import task_run
from codey.task.model import ResolveShellApproval, TaskSubmission


@pytest.mark.parametrize("denied", [False, True])
def test_text_resume_reconstructs_result_after_typed_input_discards_legacy_prompt(monkeypatch, denied):
    payload = {"call_id": "", "command": "python --version", "cwd": ".", "source_run_id": "source-1",
               "ok": not denied, "exit_code": None if denied else 0,
               "error_code": "denied" if denied else "", "truncated": False,
               "model_text": "Denied by user." if denied else "Python 3.12.8"}
    request = TaskSubmission.from_input(session_id="s", project=None, max_turns=3, provider_id="mimo",
                                         input=ResolveShellApproval("approval-1"))
    assert request.task == "Resolve the shell approval"
    work = SimpleNamespace(operation=None)
    monkeypatch.setattr(task_run, "build_run_work", lambda *_args, **_kwargs: (work, None))
    setup = SimpleNamespace(request=request, state=SimpleNamespace(
        durable=SimpleNamespace(task=lambda _: (_ for _ in ()).throw(KeyError("not admitted"))),
        resolve_shell_approval=lambda _: payload,
    ),
        session_id="s", run_id="source-1", project=None, provider_id="mimo", max_turns=3, task_kind="chat",
        task=request.task, continue_task=True, recovered_resume=False, trace=None,
        project_config_result=SimpleNamespace(config=SimpleNamespace(ignored_paths=())))
    phase, failure = task_run._build_workload(SimpleNamespace(), setup)
    assert failure is None and phase is not None
    assert setup.task == setup.request.task == "Resolve the shell approval"
    assert not hasattr(phase, "recovered_tool_outcomes")
    assert setup.request.previous_run_id == ""
