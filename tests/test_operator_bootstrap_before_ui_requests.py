"""Browser boot exchanges its credential before requesting application state."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "codey/web/assets/operator_auth.js"


@pytest.mark.parametrize("fragment,authorized", [("#codey_bootstrap=secret", True), ("", True), ("", False)])
def test_browser_removes_launch_fragment_and_checks_session(fragment, authorized):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js unavailable")
    script = f"""
const vm = require('node:vm');
const fs = require('node:fs');
const events = [];
const window = {{location: {{hash: {fragment!r}, pathname: '/', search: ''}},
  history: {{replaceState: (...args) => events.push(['replace', args[2]])}}}};
const context = vm.createContext({{window, URLSearchParams,
  fetch: async (path, options) => {{events.push(['fetch', path, options]); return {{ok: {str(authorized).lower()}}};}}}});
vm.runInContext(fs.readFileSync({str(SOURCE).replace(chr(92), '/')!r}, 'utf8'), context);
(async () => {{
  let failed = false;
  try {{await window.CodeyOperatorAuth.authorize();}} catch {{failed = true;}}
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
    html = SOURCE.parents[1].joinpath("index.html").read_text(encoding="utf-8")
    boot = html[html.index("async function boot()") :]
    assert boot.index("await window.CodeyOperatorAuth.authorize()") < boot.index("adoptCatalog()")
    assert boot.index("await window.CodeyOperatorAuth.authorize()") < boot.index("connectEvents()")


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
