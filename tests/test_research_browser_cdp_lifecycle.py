"""Real CDP ownership: task pages close, user pages and other contexts survive."""
from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from codey.automation.browser_contracts import RouteDecision
from codey.research.browser_search import BrowserSearchProvider
from codey.research.source_acquisition import with_attempt
from tests.support.real_cdp import cdp_browser

URL = "https://example.com/article"
pytestmark = pytest.mark.usefixtures("public_source_dns")


def test_task_close_releases_task_pages_and_keeps_borrowed_browser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fixture_route(self: BrowserSearchProvider, route: RouteDecision) -> None:
        route.fulfill(content_type="text/html", body="<article>Task article body with sufficient readable content.</article>")
    def http_failure(url: str) -> dict[str, object]:
        return with_attempt({"status": "error", "failure_code": "page_not_loaded"}, "http", url)
    monkeypatch.setattr(BrowserSearchProvider, "_guard_request", fixture_route)
    monkeypatch.setattr("codey.research.web_sources.fetch_http_document", http_failure)
    with cdp_browser(tmp_path / "profile") as port, sync_playwright() as observer:
        browser = observer.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        user_page = browser.contexts[0].pages[0]
        user_page.set_content("<article>User page must remain</article>")
        before = len(browser.contexts[0].pages)
        source = BrowserSearchProvider(cdp_port=port, isolated=False, launch=False, require_existing_session=True)
        try:
            assert source.fetch(URL)["status"] == "ok"
            assert source.browser.resource_binding() is not None
        finally:
            source.close()
        pump = browser.contexts[0].new_page()
        pump.close()
        assert user_page.inner_text("body") == "User page must remain"
        assert len(browser.contexts[0].pages) == before
        assert browser.is_connected()


def test_actions_do_not_touch_other_contexts_or_replacement_page(tmp_path: Path) -> None:
    from codey.automation.browser_binding import BrowserBinding, browser_endpoint, browser_identity, page_binding
    from codey.automation.browser_contracts import BrowserConfig
    from codey.automation.browser_session import BrowserSession

    with cdp_browser(tmp_path / "profile") as port, sync_playwright() as observer:
        observed = observer.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        original = observed.contexts[0].pages[0]
        target = original.context.new_page()
        other = observed.new_context().new_page()
        for page in (target, other, original):
            page.set_content("<button onclick='document.body.dataset.clicked=1'>Same name</button>")
        identity = page_binding(target, owned=True)
        binding = BrowserBinding(port, browser_identity(browser_endpoint(port)), identity.target_id, (identity,))
        interaction = BrowserSession(BrowserConfig(cdp_port=port, isolated=False, launch=False))
        interaction.restore_binding(binding)
        try:
            snapshot = interaction.snapshot()
            elements = snapshot.observation["elements"]
            assert isinstance(elements, list) and isinstance(elements[0], dict)
            assert interaction.click(str(elements[0]["ref"])).ok
            assert original.get_attribute("body", "data-clicked") is None
            assert other.get_attribute("body", "data-clicked") is None
            target.close()
            replacement = original.context.new_page()
            replacement.set_content("<button>Same name</button>")
            stale = interaction.click(str(elements[0]["ref"]))
            assert not stale.ok and stale.observation["reason"] == "browser_target_lost"
            assert not interaction.snapshot().ok
            assert replacement.get_attribute("body", "data-clicked") is None
        finally:
            interaction.close()
        assert not original.is_closed() and not other.is_closed()
