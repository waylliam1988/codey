"""Actual packaged UI, HTTP approval and subprocess execution recovery."""

from __future__ import annotations

import base64
import sys
import threading
from unittest.mock import Mock

import pytest
from playwright.sync_api import expect, sync_playwright

from codey.app import server
from codey.app.context import AppContext
from codey.app.shell_approval_state import restore_pending_shell_approvals
from tests.support.shell_approval import seed_requested_approval
from tests.test_ui_inplace_render import _goto_restored_ui


@pytest.mark.parametrize("approved", [True, False])
def test_buttons_response_loss_and_retry_preserve_single_execution(tmp_path, monkeypatch, approved):
    ctx = AppContext(tmp_path)
    script = "from pathlib import Path\nwith Path('executions').open('a') as f: f.write('executed\\n')\nprint('BROWSER_SHELL_RESULT')"
    command = f'"{sys.executable}" -c "import base64;exec(base64.b64decode(\'{base64.b64encode(script.encode()).decode()}\'))"'
    seed_requested_approval(ctx, command=command)
    restore_pending_shell_approvals(ctx)
    ctx.save_ui_state(
        {
            "active_id": "session",
            "sessions": [{"id": "session", "title": "Approval", "provider": "local", "messages": []}],
            "projects": [],
        },
        base_revision=0,
    )
    monkeypatch.setattr(server, "STATE", ctx)
    submit = Mock(side_effect=[None, "resumed"])
    monkeypatch.setattr(server, "_submit_task_after_slot_release", submit)
    httpd = server.CodeyHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                _goto_restored_ui(page, httpd.launch_url(f"http://127.0.0.1:{httpd.server_port}/"))

                # The server accepts the decision, but its response is lost.
                def drop_response(route):
                    if route.request.method == "POST":
                        route.fetch()
                        route.abort()
                    else:
                        route.continue_()

                page.route("**/api/shell_approval", drop_response)
                page.get_by_role("button", name="Allow once" if approved else "Deny", exact=True).click()
                expect(page.get_by_role("button", name="Retry continuation", exact=True)).to_be_visible()
                expect(page.get_by_role("button", name="Allow once", exact=True)).to_have_count(0)
                expect(page.locator(".shell-output")).to_contain_text(
                    "BROWSER_SHELL_RESULT" if approved else "Denied by user"
                )
                page.get_by_role("button", name="Retry continuation", exact=True).click()
                expect(page.get_by_text("Command result saved; task continued", exact=True)).to_be_visible()
                assert ctx.shell_approval_workflow.wait(5)
                assert submit.call_count == 2
                if approved:
                    assert (tmp_path / "executions").read_text().splitlines() == ["executed"]
                else:
                    assert not (tmp_path / "executions").exists()
                assert not errors
            finally:
                browser.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(5)
        ctx.close()
