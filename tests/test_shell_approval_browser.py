"""Actual packaged UI, HTTP approval and subprocess execution recovery."""

from __future__ import annotations

import base64
import sys
import threading

import pytest
from playwright.sync_api import expect, sync_playwright

from codey.app import server
from codey.app.context import AppContext
from codey.app.shell_approval_state import restore_pending_shell_approvals
from tests.support.shell_approval import seed_requested_approval
from tests.test_ui_inplace_render import _goto_restored_ui


@pytest.mark.parametrize("approved", [True, False])
def test_response_loss_and_retries_preserve_original_task_and_single_execution(tmp_path, monkeypatch, approved):
    # Observe the durable wakeup before a model worker consumes it. The
    # scheduler's execution of that checkpoint is covered by the HTTP gate.
    ctx = AppContext(tmp_path, start_scheduler=False)
    script = "from pathlib import Path\nwith Path('executions').open('a') as f: f.write('executed\\n')\nprint('BROWSER_SHELL_RESULT')"
    command = f'"{sys.executable}" -c "import base64;exec(base64.b64decode(\'{base64.b64encode(script.encode()).decode()}\'))"'
    seed_requested_approval(ctx, command=command)
    original_task = ctx.durable.task("source")
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
                        response = route.fetch()
                        assert response.status == 202
                        route.fulfill(response=response, status=502, body="{}")
                    else:
                        route.continue_()

                page.route("**/api/shell_approval", drop_response)
                with (
                    page.expect_response(
                        lambda response: response.request.method == "POST"
                        and response.url.endswith("/api/shell_approval")
                    ) as lost_response,
                    page.expect_response(
                        lambda response: response.request.method == "GET"
                        and "/api/shell_approval?id=approval" in response.url
                    ) as checked_response,
                ):
                    page.get_by_role("button", name="Allow once" if approved else "Deny", exact=True).click()
                assert lost_response.value.status == 502
                assert checked_response.value.status == 200
                expect(page.get_by_role("button", name="Allow once", exact=True)).to_have_count(0)
                expect(page.get_by_role("button", name="Deny", exact=True)).to_have_count(0)
                expect(page.locator(".shell-stdout")).to_contain_text(
                    "BROWSER_SHELL_RESULT" if approved else "Denied by user"
                )
                assert ctx.shell_approval_workflow.wait(5)
                status = ctx.shell_approval_workflow.status("approval")
                assert status["decision"] == ("allow" if approved else "deny")
                assert status["execution"] == ("settled" if approved else "not_required")
                assert status["continuation"] == "started"
                assert status["continuation_run_id"] == original_task.task_id
                task = ctx.durable.task(original_task.task_id)
                assert task.status.value == "pending"
                assert task.submission_id == original_task.submission_id
                assert task.state["resume_input"] == {
                    "kind": "resolve_shell_approval", "approval_id": "approval",
                }
                assert len(ctx.durable.tasks()) == 1
                expect(page.get_by_role("button", name="Retry continuation", exact=True)).to_have_count(0)
                expect(page.get_by_text("Task continued", exact=True)).to_have_count(0)

                page.unroute("**/api/shell_approval", drop_response)
                # Retrying a lost decision is idempotent. A second wakeup is
                # rejected because the original durable task already owns it.
                replies = page.evaluate(
                    """async approved => {
                        const post = async (path, body) => {
                            const response = await fetch(path, {
                                method: 'POST', headers: {'Content-Type': 'application/json'},
                                body: JSON.stringify(body),
                            });
                            return {status: response.status, body: await response.json()};
                        };
                        return {
                            decision: await post('/api/shell_approval', {id: 'approval', approved}),
                            retry: await post('/api/shell_approval/retry', {id: 'approval'}),
                        };
                    }""",
                    approved,
                )
                assert replies["decision"]["status"] == 202
                assert replies["decision"]["body"]["continuation"] == "started"
                assert replies["retry"]["status"] == 409
                assert replies["retry"]["body"]["code"] == "continuation_started"
                assert ctx.shell_approval_workflow.wait(5)
                assert ctx.durable.task(original_task.task_id) == task
                assert len(ctx.durable.tasks()) == 1
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
