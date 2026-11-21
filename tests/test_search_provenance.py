"""Search scope, actual acquisition identity and lossless receipt recovery."""
from __future__ import annotations

import json
from dataclasses import replace
from urllib.parse import quote_plus

import pytest
from playwright.sync_api import Route

from codey.automation.browser import open_headless_page
from codey.operations.task_execution import ExecutionDelegate
from codey.research.browser_search import BrowserSearchProvider, SearchUnavailableError
from codey.research.connector_search import ConnectorAwareSearchProvider
from codey.research.ledger import ResearchLedger
from codey.research.ledger_receipts import ledger_observation, restore_ledger_observation, search_observation
from codey.research.search_provenance import SearchAttempt, filter_site_results, query_consistency
from codey.research.source_gateway import ResearchSourceGateway
from codey.research.task_browser_source import TaskBrowserSource
from codey.research.tools import ResearchTools
from codey.research.web_sources import WebSourceProvider
from codey.runtime.core.models import ToolCall
from codey.types.json_value import JsonObject
from tests.support.real_cdp import chromium_executable
from tests.support.task_browser import browser_for


@pytest.mark.parametrize("url,allowed", [
    ("https://example.com/article", True), ("https://docs.example.com/article", True),
    ("https://EXAMPLE.COM./article", True), ("https://example.com.evil.test/article", False),
    ("https://notexample.com/article", False), ("https://example.com@evil.test/article", False),
    ("https://evil.test/?url=https://example.com", False),
])
def test_site_scope_uses_normalized_hostname_boundaries(url: str, allowed: bool) -> None:
    rows, rejected = filter_site_results("topic site:Example.COM.", [{"url": url}])
    assert bool(rows) is allowed
    assert rejected == (() if allowed else (url,))


def test_site_scope_normalizes_idna_and_never_claims_missing_query_is_matched() -> None:
    rows, rejected = filter_site_results("site:bücher.example", [
        {"url": "https://xn--bcher-kva.example/a"}, {"url": "https://bücher.example.evil.test/a"}])
    assert len(rows) == 1 and len(rejected) == 1
    assert query_consistency("https://www.bing.com/search", "query") is None
    assert query_consistency("https://www.bing.com/search?q=old", "query") is False


BODY = ('<nav><a href="https://example.com/nav">Navigation</a></nav>'
    '<a href="https://example.com/arbitrary">Unscoped link</a>'
    '<main>Official documentation search results for the requested query.'
    '<li class="b_algo" data-ad="true"><h2><a href="https://example.com/adrow">Sponsored</a></h2></li>'
    '<div data-ad="true"><a href="https://example.com/ad">Advertisement</a></div>'
    '<a href="https://example.com.evil.test/wrong">Wrong host result</a>'
    '<a href="https://docs.example.com/current">Current source</a></main>')


@pytest.mark.parametrize("legacy", [False, True])
def test_both_search_entries_filter_fallback_and_preserve_provenance(legacy: bool) -> None:
    query = "current docs site:example.com"
    ledger = ResearchLedger()
    if legacy:
        session = open_headless_page(browser_path=chromium_executable())
        try:
            def fulfill(route: Route) -> None:
                route.fulfill(content_type="text/html", body=BODY)
            session.page.route("**/*", fulfill)
            source = BrowserSearchProvider(engine="bing")
            rows = source._search_page_results_on_browser_thread(session.page, query, 8)
            ledger.record_search(query, rows, attempts=source.search_attempts)
        finally:
            session.close()
    else:
        browser = browser_for(lambda route: route.fulfill(content_type="text/html", body=BODY))
        try:
            web = WebSourceProvider()
            web.bind_browser_source(TaskBrowserSource(browser, engine="bing"))
            wrapped = ConnectorAwareSearchProvider(web, connector_ids=())
            outcome = ResearchSourceGateway(wrapped, ledger).search(query)
            assert not outcome.error
            rows = list(outcome.hits)
        finally:
            browser.close()
    assert [row["url"] for row in rows] == ["https://docs.example.com/current"]
    attempt = ledger.searches[-1].attempts[-1]
    assert attempt.requested_query == query and attempt.engine == "bing"
    assert attempt.final_url == "https://www.bing.com/search?q=" + quote_plus(query)
    assert attempt.query_consistent is True and attempt.extraction_scope == "main_anchors"
    assert attempt.rejected_urls == ("https://example.com.evil.test/wrong",)
    restored = ResearchLedger()
    restore_ledger_observation(restored, ledger_observation(ledger, "web_search"))
    assert restored.searches == ledger.searches


@pytest.mark.parametrize("redirect", [False, True])
def test_task_search_rejects_old_query_without_returning_stale_dom(redirect: bool) -> None:
    body = BODY + ('<script>history.replaceState(null,"","/search?q=old");</script>' if redirect else
        '<input name="q" value="old">')
    browser = browser_for(lambda route: route.fulfill(content_type="text/html", body=body))
    try:
        source = TaskBrowserSource(browser, engine="bing")
        with pytest.raises(SearchUnavailableError, match="search_query_mismatch"):
            source.search("new query")
        attempt = source.search_attempts[-1]
        assert attempt.query_consistent is False and attempt.accepted_urls == ()
        assert attempt.failure_kind == "search_query_mismatch"
    finally:
        browser.close()


def test_empty_hits_keep_out_of_scope_diagnostics_and_old_receipts_restore_as_unknown() -> None:
    ledger = ResearchLedger()
    ledger.record_search("site:example.com", [], attempts=(SearchAttempt("site:example.com", "bing",
        rejected_urls=("https://evil.test/a",)),))
    payload = ledger_observation(ledger, "web_search")
    assert search_observation(payload) == ledger.searches[-1]
    raw = payload["search"]
    assert isinstance(raw, dict)
    del raw["attempts"]
    assert search_observation(payload) == replace(ledger.searches[-1], attempts=())
    raw["attempts"] = [{"invented": True}]
    with pytest.raises(ValueError, match="provenance"):
        search_observation(payload)


def test_gateway_filters_uninstrumented_providers_and_records_unknown_provenance() -> None:
    class Provider:
        def search(self, query: str, *, limit: int) -> list[JsonObject]:
            return [{"url": "https://example.com.evil.test/a", "title": "Wrong"}]
        def fetch(self, url: str) -> dict[str, object]:
            raise AssertionError("not a source read")
    ledger = ResearchLedger()
    outcome = ResearchSourceGateway(Provider(), ledger).search("site:example.com")
    assert not outcome.error and not outcome.hits
    attempt = ledger.searches[-1].attempts[-1]
    assert attempt.query_consistent is None and attempt.rejected_urls
    json.dumps(ledger_observation(ledger, "web_search"))


def test_failed_search_keeps_attempt_diagnostics_without_establishing_source_evidence() -> None:
    class FailedSearch:
        search_attempts = (SearchAttempt("new query", "bing", "https://www.bing.com/search?q=old",
            query_consistent=False, failure_kind="search_query_mismatch"),)
        def search(self, query: str, *, limit: int) -> list[JsonObject]:
            raise SearchUnavailableError("search_query_mismatch", engine="bing")
        def fetch(self, url: str) -> dict[str, object]:
            raise AssertionError("no source read")
    research = ResearchTools(FailedSearch(), None, None)
    result, success, _ = ExecutionDelegate(research_tools=research).execute(ToolCall("web_search", {"query": "new query"}))
    assert not success and not result.ok and not research.ledger.searches
    assert "research_observation" not in result.canonical
    attempts = result.canonical["search_attempts"]
    assert isinstance(attempts, list) and isinstance(attempts[0], dict)
    assert attempts[0]["failure_kind"] == "search_query_mismatch"
