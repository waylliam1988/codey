"""An approval decision is not the end of the continued task."""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from codey.app.shell_service import build_shell_approval_continuation
from tools.frontend_source import frontend_source
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


@pytest.mark.parametrize("approved", [False, True])
def test_http_approval_reconciles_continuation_without_sse(approved):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js unavailable")
    source = frontend_source("shell_approvals").replace("export function ", "function ")
    script = "const assert = require('node:assert/strict');\n" + source + f"""
(async () => {{
  const session = {{id:'session',messages:[{{type:'shell_request',id:'approval-1'}}]}};
  const controller = createApprovalController({{
    sessions:()=>[session], changed:()=>{{}},
    fetch:async()=>({{ok:true,json:async()=>({{type:'shell_approval_status',id:'approval-1',session_id:'session',
      decision:{json.dumps('allow' if approved else 'deny')},execution:'settled',continuation:'started',revision:5}})}}),
  }});
  await controller.submit('approval-1', {json.dumps(approved)});
  assert.equal(session.messages.length, 1);
  assert.equal(session.messages[0].approval.continuation, 'started');
  assert.equal(controller.decided('approval-1'), true);
}})().catch(error => {{console.error(error); process.exitCode = 1;}});
"""
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=10)
    assert result.returncode == 0, result.stderr

def test_interrupted_continuation_does_not_claim_command_completed():
    prompt = build_shell_approval_continuation(command='echo pending', result={
        'status': 'stopped', 'ok': False, 'exit_code': None, 'output': 'Application closed before command execution.'})
    assert 'interrupted' in prompt
    assert 'approved and ran' not in prompt
