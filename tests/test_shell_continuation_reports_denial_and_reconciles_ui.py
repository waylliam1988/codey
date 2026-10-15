"""An approval decision is not the end of the continued task."""
from __future__ import annotations

import json

from codey.app.shell_service import build_shell_approval_continuation
from tools.ui_e2e import ScriptedWriter


def test_denied_continuation_does_not_claim_execution():
    prompt = build_shell_approval_continuation(
        command="git status --short",
        result={"status": "denied", "ok": False, "exit_code": None, "output": "Denied by user."},
    )
    assert "The user denied this shell command; it was not executed:" in prompt
    assert "approved and ran" not in prompt
    assert "Do not retry the denied command without new user authorization." in prompt
    assert "Denied by user." in prompt
    reply = json.loads(ScriptedWriter().send(prompt))
    assert reply["args"]["summary"] == "denial continuation completed"


def test_interrupted_continuation_does_not_claim_command_completed():
    prompt = build_shell_approval_continuation(command='echo pending', result={
        'status': 'stopped', 'ok': False, 'exit_code': None, 'output': 'Application closed before command execution.'})
    assert 'interrupted' in prompt
    assert 'approved and ran' not in prompt
