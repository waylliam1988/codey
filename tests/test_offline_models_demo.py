"""The inspection page serves real assets and cannot contact a model service."""

import threading
from http.server import ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

from tests.test_ui_workflow import ui_browser as ui_browser
from tools.offline_models_demo import ModelsDemo, ModelsHandler
from tools.offline_retry_demo import Demo, Handler


@pytest.fixture(params=[(ModelsDemo, ModelsHandler), (Demo, Handler)], ids=["models", "retry"])
def demo_page(request, ui_browser):
    demo, handler = request.param
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    httpd.demo = demo()
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    page = ui_browser[0].new_page()
    external = []

    def loopback_only(route):
        if urlsplit(route.request.url).hostname != "127.0.0.1":
            external.append(route.request.url)
            route.abort()
        else:
            route.continue_()

    page.route("**/*", loopback_only)
    page.goto(f"http://127.0.0.1:{httpd.server_port}/")
    try:
        yield page, httpd.demo
        assert external == []
    finally:
        page.close()
        httpd.shutdown()
        httpd.server_close()
        worker.join(2)


def test_inspection_uses_real_model_controls_and_keeps_retry_usable(demo_page):
    page, demo = demo_page
    expect(page.locator("#provider-button")).to_be_enabled()
    if isinstance(demo, ModelsDemo):
        page.locator("#btn-settings").click()
        expect(page.locator(".model-source-toggle")).to_have_count(3)
        page.get_by_role("checkbox", name="Enable Websites", exact=True).uncheck()
        page.locator("#model-settings-save").click()
        expect(page.locator("#local-config-pop")).to_be_hidden()
        page.locator("#task").fill("offline draft")
        expect(page.locator("#send")).to_be_disabled()
        page.locator("#btn-settings").click()
        local = page.locator('.model-source[data-source-id="local"]')
        local.locator("details > summary").first.click()
        local.get_by_role("button", name="Refresh models", exact=True).click()
        expect(local.get_by_role("checkbox", name="Use New arrival", exact=True)).not_to_be_checked()
    else:
        expect(page.get_by_role("button", name="Retry", exact=True)).to_be_enabled()
