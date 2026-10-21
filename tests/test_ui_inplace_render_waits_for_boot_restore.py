"""DOM test setup waits for boot restoration, not just declared JS functions."""

from urllib.parse import urlsplit

import pytest

from tests import test_ui_inplace_render as rendering
from tests.test_ui_workflow import ui_browser as ui_browser


def test_dom_harness_does_not_start_before_delayed_state_restore(ui_browser, monkeypatch):
    browser, url = ui_browser
    page = browser.new_page()
    pending = []
    waiting_for_restore = False
    state = {"active_id": "restored", "projects": [], "sessions": [
        {"id": "restored", "title": "Restored", "provider": "deepseek", "messages": []}]}

    def hold_restore(route):
        if route.request.method == "GET":
            if waiting_for_restore:
                route.fulfill(json={"state": state})
            else:
                pending.append(route)
        else:
            route.fulfill(json={"ok": True})

    page.route("**/api/ui_state", hold_restore)
    real_goto = page.goto
    real_wait = page.wait_for_function

    def goto(url):
        with page.expect_request(lambda request: request.method == "GET"
                                 and urlsplit(request.url).path == "/api/ui_state"):
            return real_goto(url)

    def wait(expression, **options):
        nonlocal waiting_for_restore
        # Only release the response once the harness waits for restoration.
        # Function-presence waits leave it pending and fail the assertion below.
        if "__inPlaceRestoreDone" in expression:
            waiting_for_restore = True
            for route in pending:
                route.fulfill(json={"state": state})
            pending.clear()
        return real_wait(expression, **options)

    monkeypatch.setattr(page, "goto", goto)
    monkeypatch.setattr(page, "wait_for_function", wait)
    try:
        rendering._goto_restored_ui(page, url)
        assert page.evaluate("window.__inPlaceRestoreDone === true"), "DOM tests started before state restoration"
        assert page.evaluate("UiState.current().active_id") == "restored"
        # Add through the application model so a late history reconciliation
        # cannot erase a DOM-only node after boot restoration completes.
        page.evaluate("addToSession('restored', "
                      "{type:'asst', text:'| Name | Result |\\n| --- | --- |\\n| sample | OK |'})")
        assert page.locator("#chat table").count() == 1
    finally:
        for route in pending:
            route.abort()
        page.close()


@pytest.mark.parametrize("blocked_path", ["/api/operator_session", "/api/ui_state"])
def test_boot_failure_diagnostics_identify_failed_or_pending_stage(ui_browser, monkeypatch, blocked_path):
    browser, url = ui_browser
    page = browser.new_page()
    pending = []

    def api(route):
        path = urlsplit(route.request.url).path
        if path == blocked_path:
            if path == "/api/operator_session":
                route.fulfill(status=403, json={"error": "Unauthorized"})
            else:
                pending.append(route)
        else:
            route.fulfill(json={"ok": True})

    page.route("**/api/**", api)
    real_goto = page.goto
    real_wait = page.wait_for_function

    def goto(url):
        # Start the short diagnostic timeout only after the injected fault is reached.
        with page.expect_request(lambda request: urlsplit(request.url).path == blocked_path):
            return real_goto(url)

    def short_wait(expression, **options):
        return real_wait(expression, timeout=1000, **options)

    monkeypatch.setattr(page, "goto", goto)
    monkeypatch.setattr(page, "wait_for_function", short_wait)
    diag = rendering._attach_page_diagnostics(page)
    try:
        with pytest.raises(AssertionError, match="UI boot failed") as failure:
            rendering._goto_restored_ui(page, url, diag=diag)
        assert diag["goto_status"] == 200
        assert diag["phase"] == "boot_restore"
        assert diag["page"]["uiStateAvailable"] is True
        assert diag["page"]["restoreStarted"] == (blocked_path == "/api/ui_state")
        assert diag["page"]["restoreDone"] is False
        assert blocked_path in str(failure.value)
        assert "codey_bootstrap" not in str(failure.value)
        assert failure.value.__cause__ is not None
        if blocked_path == "/api/operator_session":
            assert "403 /api/operator_session" in diag["bad_responses"]
            assert "authorize this browser" in diag["page"]["status"]
        else:
            assert f"GET {blocked_path}" in diag["pending_requests"]
    finally:
        for route in pending:
            route.abort()
        page.close()
