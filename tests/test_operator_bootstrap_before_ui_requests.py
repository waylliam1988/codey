"""Browser boot exchanges its credential before requesting application state."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tools.frontend_source import frontend_source

SOURCE = Path(__file__).resolve().parents[1] / "frontend/src/operator_auth.ts"


@pytest.mark.parametrize("fragment,authorized", [("#codey_bootstrap=secret", True), ("", True), ("", False)])
def test_browser_removes_launch_fragment_and_checks_session(fragment, authorized):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js unavailable")
    source = frontend_source("operator_auth")
    script = f"""
const vm = require('node:vm');
const fs = require('node:fs');
const events = [];
const window = {{location: {{hash: {fragment!r}, pathname: '/', search: ''}},
  history: {{replaceState: (...args) => events.push(['replace', args[2]])}}}};
const context = vm.createContext({{window, URLSearchParams,
  fetch: async (path, options) => {{events.push(['fetch', path, options]); return {{ok: {str(authorized).lower()}}};}}}});
    vm.runInContext({json.dumps(source)}, context);
(async () => {{
  let failed = false;
  try {{await window.OperatorAuth.authorize();}} catch {{failed = true;}}
  if (failed === {str(authorized).lower()}) throw new Error('wrong auth result');
  if ({str(bool(fragment)).lower()}) {{
    if (events[0][0] !== 'replace' || events[0][1] !== '/') throw new Error('credential not cleared first');
    if (events[1][2].method !== 'POST') throw new Error('no exchange');
  }} else if (events[0][2].method) throw new Error('must probe existing cookie');
}})().catch(error => {{console.error(error); process.exitCode = 1;}});
"""
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_real_ui_boot_awaits_operator_auth_before_catalog_or_sse():
    boot = frontend_source("legacy-runtime")
    assert boot.index("await window.OperatorAuth.authorize()") < boot.index("adoptCatalog()")
    assert boot.index("await window.OperatorAuth.authorize()") < boot.index("connectEvents();")


def test_expired_bootstrap_and_concurrent_exchange_are_fail_closed():
    from concurrent.futures import ThreadPoolExecutor

    from codey.app.operator_auth import BOOTSTRAP_LIFETIME_SECONDS, OperatorAuth

    now = [0.0]
    auth = OperatorAuth(1234, clock=lambda: now[0])
    token = auth.issue_bootstrap()
    now[0] = BOOTSTRAP_LIFETIME_SECONDS
    assert auth.exchange(token) is None
    token = auth.issue_bootstrap()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(auth.exchange, [token] * 4))
    assert sum(cookie is not None for cookie in results) == 1
