"""An approval decision is not the end of the continued task."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

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


@pytest.mark.parametrize("approved", [False, True])
def test_http_approval_reconciles_continuation_without_sse(approved):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js unavailable")
    html = (Path(__file__).parents[1] / "codey/web/index.html").read_text(encoding="utf-8")
    start = html.index("async function approveCommand(")
    source = html[start:html.index("// ============================ composer", start)]
    script = """
const assert = require('node:assert/strict');
const calls = [];
const fetch = async () => ({ok: true, json: async () => ({event: {type: 'shell_result'}, continued: true})});
const handleServerEvent = event => calls.push(event.type);
const reconcileRunState = async () => calls.push('state');
const showCommandApprovalError = () => {throw new Error('unexpected error');};
const showCommandContinuationError = () => {throw new Error('unexpected busy error');};
""" + source + f"""
(async () => {{
  await approveCommand('approval-1', {json.dumps(approved)}, {{}}, {{}}, null);
  assert.deepEqual(calls, ['shell_result', 'state']);
}})().catch(error => {{console.error(error); process.exitCode = 1;}});
"""
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=10)
    assert result.returncode == 0, result.stderr
