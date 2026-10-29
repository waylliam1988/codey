"""Search recovery covers page creation as well as navigation failures."""
from types import SimpleNamespace
from unittest import mock

import pytest

from codey.research import browser_search
from codey.research.browser_search import BrowserSearchProvider
from codey.runtime.core import cancellation


def test_next_search_replaces_closed_session_seed_page():
    closed = mock.Mock()
    closed.is_closed.return_value = True
    replacement = mock.Mock()
    replacement._codey_research_guarded = False
    context = SimpleNamespace(new_page=mock.Mock(return_value=replacement))
    closed.context = context
    provider = BrowserSearchProvider()
    provider._session = SimpleNamespace(page=closed, browser=SimpleNamespace())

    assert provider._ensure_search_page_on_browser_thread() is replacement
    context.new_page.assert_called_once_with()
    replacement.route.assert_called_once()


def test_closed_context_during_page_preparation_reconnects_and_retries():
    provider = BrowserSearchProvider()
    provider._ensure_search_page_on_browser_thread = mock.Mock(
        side_effect=RuntimeError("Page.route: Target page, context or browser has been closed")
    )
    provider._replace_search_page_on_browser_thread = mock.Mock(return_value=object())
    provider._close_on_browser_thread = mock.Mock()
    provider._discard_search_page_on_browser_thread = mock.Mock()
    hits = [{"title": "Recovered", "url": "https://example.com/report"}]
    provider._search_page_results_on_browser_thread = mock.Mock(return_value=hits)

    assert provider._search_on_browser_thread("helium", 3) == hits
    provider._close_on_browser_thread.assert_called_once_with()
    assert len(provider.last_search_errors) == 1


def test_disconnected_browser_is_reopened():
    provider = BrowserSearchProvider()
    old = mock.Mock()
    old.browser.is_connected.return_value = False
    provider._session = old
    reopened = object()
    with mock.patch.object(browser_search, "open_chat_page", return_value=reopened):
        assert provider._ensure_session_on_browser_thread() is reopened
    old.close.assert_called_once_with()


def test_first_engine_timeout_reserves_time_for_browser_fallback():
    provider = BrowserSearchProvider()
    provider._engine_order = ("bing", "duckduckgo")
    provider._ensure_search_page_on_browser_thread = mock.Mock(return_value=object())
    provider._replace_search_page_on_browser_thread = mock.Mock(return_value=object())
    provider._discard_search_page_on_browser_thread = mock.Mock()
    now = [0.0]
    engines = []
    hits = [{"title": "Recovered", "url": "https://example.com/report"}]

    def search_page(*_args, engine, deadline, **_kwargs):
        engines.append(engine)
        if engine == "bing":
            assert 0 < deadline < browser_search._SEARCH_TOTAL_TIMEOUT_SECONDS
            now[0] = deadline
            raise TimeoutError("navigation timed out")
        return hits

    with (
        mock.patch.object(browser_search.time, "monotonic", side_effect=lambda: now[0]),
        mock.patch.object(provider, "_search_page_results_on_browser_thread", side_effect=search_page),
    ):
        assert provider._search_on_browser_thread("helium", 3) == hits
    assert engines == ["bing", "duckduckgo"]


@pytest.mark.parametrize("error", [cancellation.TaskCancelled("stop"), cancellation.DeadlineExceeded("expired")])
def test_cancellation_during_page_creation_does_not_retry(error):
    provider = BrowserSearchProvider()
    provider._ensure_search_page_on_browser_thread = mock.Mock(side_effect=error)
    provider._replace_search_page_on_browser_thread = mock.Mock()
    with pytest.raises(type(error)):
        provider._search_on_browser_thread("helium", 3)
    provider._replace_search_page_on_browser_thread.assert_not_called()


def test_failed_query_does_not_poison_the_next_query_on_the_same_provider():
    context = SimpleNamespace()

    def new_page():
        page = mock.Mock(context=context, _codey_research_guarded=False)
        page.is_closed.return_value = False
        page.close.side_effect = lambda: setattr(page.is_closed, "return_value", True)

        def route(*_args):
            if page.is_closed():
                raise RuntimeError("Page.route: Target page, context or browser has been closed")

        page.route.side_effect = route
        return page

    context.new_page = mock.Mock(side_effect=new_page)
    seed = new_page()
    provider = BrowserSearchProvider(engine="bing")
    provider._session = SimpleNamespace(page=seed, browser=SimpleNamespace())
    hits = [{"title": "Recovered", "url": "https://example.com/report"}]

    def search_page(_page, query, *_args, **_kwargs):
        if query == "first query":
            raise TimeoutError("navigation timed out")
        assert not _page.is_closed()
        return hits

    with mock.patch.object(provider, "_search_page_results_on_browser_thread", side_effect=search_page):
        with pytest.raises(browser_search.SearchUnavailableError):
            provider._search_on_browser_thread("first query", 3)
        assert seed.is_closed()
        assert provider._search_on_browser_thread("next query", 3) == hits


def test_persistent_page_creation_failure_stops_with_a_classified_error():
    provider = BrowserSearchProvider()
    failure = RuntimeError("Page.route: Target page, context or browser has been closed")
    provider._ensure_search_page_on_browser_thread = mock.Mock(side_effect=failure)
    provider._replace_search_page_on_browser_thread = mock.Mock(side_effect=failure)
    provider._close_on_browser_thread = mock.Mock()

    with pytest.raises(browser_search.SearchUnavailableError):
        provider._search_on_browser_thread("helium", 3)

    attempts = 1 + provider._replace_search_page_on_browser_thread.call_count
    assert attempts == len(provider._engine_order) * browser_search._SEARCH_NAV_ATTEMPTS
    assert len(provider.last_search_errors) == attempts


def test_result_blocks_can_arrive_after_navigation_without_a_false_empty_result():
    provider = BrowserSearchProvider(engine="bing")
    page = mock.Mock(url="https://www.bing.com/search?q=database")
    link = mock.Mock()
    link.get_attribute.return_value = "https://sqlite.org/wal.html"
    link.inner_text.return_value = "SQLite WAL"
    block = mock.Mock()
    block.query_selector.return_value = link
    blocks = []

    def appeared(*_args, **kwargs):
        assert kwargs["state"] == "attached"
        assert 0 < kwargs["timeout"] <= 2500
        blocks.append(block)

    page.wait_for_selector.side_effect = appeared
    page.query_selector_all.side_effect = lambda _selector: blocks
    hits = provider._search_page_results_on_browser_thread(page, "database", 3)
    assert hits[0]["url"] == "https://sqlite.org/wal.html"
    page.goto.assert_called_once()
