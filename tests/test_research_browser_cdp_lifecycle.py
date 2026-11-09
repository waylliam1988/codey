"""Production provider and Session over a dedicated real CDP browser."""
from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from playwright.sync_api import Page, Route, sync_playwright

from codey.automation.browser import BrowserTargetBindingError, CdpEndpoint, Session
from codey.research.browser_interaction import BrowserInteraction
from codey.research.browser_search import _search_browser_call
from codey.research.resumable_browser import ResumableBrowserSearchProvider
from tests.support.real_cdp import cdp_browser

URL = "https://example.com/article"
pytestmark = pytest.mark.usefixtures("public_source_dns")


def test_task_close_releases_task_pages_and_keeps_borrowed_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    with cdp_browser(tmp_path / "profile") as port:
        # Discovery is pinned to our owned endpoint; CDP, pages and teardown are real.
        def endpoint(**kwargs: object) -> CdpEndpoint:
            return CdpEndpoint(port, None)

        def fixture_route(_self: object, route: Route) -> None:
            route.fulfill(status=200, content_type="text/html",
                          body="<html><body><article>Task article body with sufficient readable content.</article></body></html>")

        monkeypatch.setattr("codey.automation.browser._ensure_cdp_endpoint", endpoint)
        monkeypatch.setattr("codey.research.browser_search.BrowserSearchProvider._guard_request", fixture_route)
        with sync_playwright() as observer:
            browser = observer.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            user_page = browser.contexts[0].pages[0]
            user_page.set_content("<article>User page must remain</article>")
            before = len(browser.contexts[0].pages)
            provider = ResumableBrowserSearchProvider(headless=False, isolated=False, cdp_port=port)
            try:
                fetched = provider.fetch(URL)
                assert fetched["status"] == "ok", fetched
            finally:
                provider.close()
            observer_page = browser.contexts[0].new_page()
            observer_page.close()  # Pump the observer's CDP transport after task teardown.
            assert user_page.inner_text("body") == "User page must remain"
            assert len(browser.contexts[0].pages) == before
            assert browser.is_connected()


def test_new_browser_on_same_port_cannot_satisfy_old_binding(tmp_path: Path) -> None:
    with sync_playwright() as pw:
        with cdp_browser(tmp_path / "first") as port:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            old_session = Session(pw, browser, browser.contexts[0].pages[0])
            old_binding = old_session.bind_page()
        with cdp_browser(tmp_path / "second", port=port):
            replacement = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            new_session = Session(pw, replacement, replacement.contexts[0].pages[0])
            with pytest.raises(BrowserTargetBindingError):
                new_session.assert_bound(old_binding)


def test_failed_search_keeps_borrowed_user_page(tmp_path: Path) -> None:
    with cdp_browser(tmp_path / "profile") as port:
        def exercise_borrowed_page() -> None:
            pw = sync_playwright().start()
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            page = browser.contexts[0].pages[0]
            provider = ResumableBrowserSearchProvider(headless=False, isolated=False)
            provider._session = Session(pw, browser, page)
            provider._search_page = page
            try:
                provider._discard_search_page_on_browser_thread(page)
                assert page.is_closed() is False
                assert provider._search_page is None
            finally:
                provider._close_on_browser_thread()

        _search_browser_call(exercise_borrowed_page)
        with sync_playwright() as observer:
            browser = observer.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            assert len(browser.contexts[0].pages) == 1


def test_actions_do_not_touch_other_contexts_or_replacement_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    with cdp_browser(tmp_path / "profile") as port:
        def connect() -> Session:
            pw = sync_playwright().start()
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            return Session(pw, browser, browser.contexts[0].pages[0])

        session = _search_browser_call(connect)
        provider = ResumableBrowserSearchProvider(headless=False, isolated=False, cdp_port=port)
        provider._session = session

        def prepare() -> None:
            target = session.page.context.new_page()
            other = session.browser.new_context().new_page()
            for page in (target, other, session.page):
                page.set_content("<button onclick='document.body.dataset.clicked=1'>Same name</button>")
            provider._fetch_page = target

        _search_browser_call(prepare)
        interaction = BrowserInteraction(provider)
        try:
            snapshot = interaction.snapshot()
            elements = snapshot.observation["elements"]
            assert isinstance(elements, list) and isinstance(elements[0], dict)
            assert interaction.click(str(elements[0]["ref"])).ok

            def inspect_and_replace() -> None:
                assert session.page.get_attribute("body", "data-clicked") is None
                assert session.browser.contexts[1].pages[0].get_attribute("body", "data-clicked") is None
                cast(Page, provider._fetch_page).close()
                provider._fetch_page = session.page.context.new_page()
                provider._fetch_page.set_content("<button>Same name</button>")

            _search_browser_call(inspect_and_replace)
            with pytest.raises(BrowserTargetBindingError):
                interaction.snapshot()
        finally:
            provider.close()
