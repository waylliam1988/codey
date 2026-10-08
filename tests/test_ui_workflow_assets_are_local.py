"""UI behavior fixtures load shipped assets without a loopback asset transport."""
from urllib.parse import urlsplit

from codey.app import server
from tests.test_ui_workflow import page as workflow_page
from tests.test_ui_workflow import ui_browser as ui_browser


def test_workflow_boot_uses_shipped_assets_when_asset_connections_are_unavailable(ui_browser, monkeypatch):
    browser, url = ui_browser
    asset_requests = []
    real_get = server.Handler.do_GET
    real_new_page = browser.new_page

    def get(handler):
        if urlsplit(handler.path).path.startswith("/assets/"):
            asset_requests.append(handler.path)
            if urlsplit(handler.path).path == "/assets/conversation_ui.js":
                handler.close_connection = True
                return
        real_get(handler)

    def new_page(**options):
        page = real_new_page(**options)
        real_wait = page.wait_for_function

        def wait(expression, **kwargs):
            return real_wait(expression, **{**kwargs, "timeout": 1500})

        monkeypatch.setattr(page, "wait_for_function", wait)
        return page

    monkeypatch.setattr(server.Handler, "do_GET", get)
    monkeypatch.setattr(browser, "new_page", new_page)
    fixture = workflow_page.__wrapped__((browser, url))
    try:
        page = next(fixture)
        assert page.evaluate("CodeyUiState.current().active_id") == "a"
        assert page.evaluate("typeof CodeyConversationUI.init") == "function"
        assert asset_requests == []
    finally:
        fixture.close()
