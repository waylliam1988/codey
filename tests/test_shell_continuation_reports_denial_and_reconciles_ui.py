"""An approval decision is not the end of the continued task."""
from __future__ import annotations

import json

from codey.app.shell_service import build_shell_approval_continuation
from codey.utils.object_fields import object_fields
from tools.ui_e2e import ScriptedWriter


def test_denied_continuation_does_not_claim_execution() -> None:
    prompt = build_shell_approval_continuation(
        command="git status --short",
        result={"status": "denied", "ok": False, "exit_code": None, "output": "Denied by user."},
    )
    assert "The user denied this shell command; it was not executed:" in prompt
    assert "approved and ran" not in prompt
    assert "Do not retry the denied command without new user authorization." in prompt
    assert "Denied by user." in prompt
    reply: object = json.loads(ScriptedWriter().send(prompt))
    assert object_fields(reply)
    args = reply["args"]
    assert object_fields(args)
    assert args["summary"] == "denial continuation completed"


def test_interrupted_continuation_does_not_claim_command_completed() -> None:
    prompt = build_shell_approval_continuation(command='echo pending', result={
        'status': 'stopped', 'ok': False, 'exit_code': None, 'output': 'Application closed before command execution.'})
    assert 'interrupted' in prompt
    assert 'approved and ran' not in prompt


def test_spawn_failure_continuation_reports_that_the_command_did_not_start() -> None:
    prompt = build_shell_approval_continuation(
        command="echo pending",
        result={"status": "spawn_error", "ok": False, "exit_code": None, "error": "interpreter missing"},
    )
    assert "the process did not start" in prompt
    assert "approved and ran" not in prompt
    assert "interpreter missing" in prompt
