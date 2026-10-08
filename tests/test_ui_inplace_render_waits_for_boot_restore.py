"""DOM test setup waits for boot restoration, not just declared JS functions."""

from urllib.parse import urlsplit

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
        assert page.evaluate("CodeyUiState.current().active_id") == "restored"
        page.evaluate("appendMessageNode(document.getElementById('chat'), "
                      "{type:'asst', text:'| Name | Result |\\n| --- | --- |\\n| sample | OK |'})")
        assert page.locator("#chat table").count() == 1
    finally:
        for route in pending:
            route.abort()
        page.close()
