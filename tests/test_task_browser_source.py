"""Search and source admission through the task-owned Chromium process."""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from codey.automation.browser_contracts import BrowserToolOutput, RouteDecision
from codey.automation.browser_session import BrowserSession
from codey.research import source_download
from codey.research.browser_search import SearchUnavailableError
from codey.research.source_acquisition import acquisition_observation
from codey.research.task_browser_source import TaskBrowserSource
from tests.support.task_browser import browser_for


def test_source_only_authorization_borrows_the_task_worker_without_exposing_actions() -> None:
    from codey.operations.kernel_context import KernelContext
    from codey.operations.kernel_contracts import KernelExecutionDeps, KernelRunRequest, KernelTransportDeps
    from codey.operations.kernel_protocol import build_turn_snapshot
    from codey.operations.provider_session import adapt_provider
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.research.tools import ResearchTools
    from codey.research.web_sources import WebSourceProvider
    from tests.support.kernel_fixture import runtime

    class Provider:
        def send(self, prompt: str) -> str:
            raise AssertionError("this resource check must not generate")

    browser = browser_for(lambda route: route.fulfill(body="<article>Acquired source text.</article>"))
    source = WebSourceProvider()
    tools = ResearchTools(source, None, None)
    session = TaskSession(TaskPolicy(frozenset({"control", "web.read"})))
    try:
        with runtime() as sink:
            context = KernelContext(session, KernelRunRequest(
                transport=KernelTransportDeps(adapt_provider(Provider()), run_id=sink.run_id),
                execution=KernelExecutionDeps(intent_sink=sink, browser_session=browser, research_tools=tools)))
            assert context.durable.tool_resources.task_browser(sink.run_id) is browser
            assert isinstance(source.browser_source, TaskBrowserSource) and source.browser_source.browser is browser
            assert "browser_click" not in build_turn_snapshot(session).inventory_names
    finally:
        browser.close()


@pytest.mark.parametrize("fallback", [False, True])
def test_search_fallback_preserves_query_and_ignores_navigation(fallback: bool) -> None:
    visited: list[str] = []

    def route(reply: RouteDecision) -> None:
        visited.append(reply.request.url)
        body = ('<main>Access denied. Are you a robot?</main>' if fallback and 'bing.com' in reply.request.url else
            '<nav><a href="https://example.com/navigation">Navigation</a></nav>'
            '<main>Results for the requested official documentation query.'
            '<a href="https://example.com/current">Current source document</a>'
            '<a href="https://example.com/current">Duplicate</a>'
            '<a href="https://www.bing.com/search?q=other">Next</a></main>')
        reply.fulfill(content_type="text/html", body=body)

    browser = browser_for(route)
    try:
        source = TaskBrowserSource(browser)
        rows = source.search('frame identity site:example.com')
        assert [row['url'] for row in rows] == ['https://example.com/current']
        assert all(parse_qs(urlparse(url).query)['q'] == ['frame identity site:example.com'] for url in visited)
        assert len(visited) == (2 if fallback else 1)
        assert bool(source.last_search_errors) == fallback
        binding = browser.resource_binding()
        source.close()
        assert browser.snapshot().ok and browser.resource_binding() == binding
    finally:
        browser.close()


def test_search_rejects_wrong_query_and_classifies_challenge_without_minting_results() -> None:
    browser = browser_for(lambda route: route.fulfill(content_type="text/html",
        body='<p>Access denied, are you a robot?</p><a href="https://example.com/not-a-result">Help</a>'))
    try:
        source = TaskBrowserSource(browser, engine='bing')
        with pytest.raises(SearchUnavailableError) as caught:
            source.search('current query')
        assert caught.value.failure_kind == 'search_page_unavailable'
        assert source.last_search_failure['engine'] == 'bing'
        assert browser.snapshot().ok
    finally:
        browser.close()


def test_failed_navigation_translates_dispatch_into_the_source_delivery_contract() -> None:
    browser = browser_for(lambda route: route.abort())
    try:
        page = TaskBrowserSource(browser).fetch('https://example.com/failed')
        assert page['status'] == 'error'
        trace = acquisition_observation(page['acquisition'])
        attempts = trace['attempts']
        assert isinstance(attempts, list) and len(attempts) == 1
        attempt = attempts[0]
        assert isinstance(attempt, dict) and attempt['delivery'] == 'unconfirmed'
        assert browser.resource_binding() is not None
    finally:
        browser.close()


def test_pdf_discovery_and_download_keep_both_original_acquisition_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    browser = BrowserSession()
    url = 'https://example.com/source.pdf'

    def navigate(target: str) -> BrowserToolOutput:
        assert target == url
        return BrowserToolOutput('Opened PDF', True, {})

    def capture() -> dict[str, object]:
        return {'status': 'ok', 'url': url, 'http_status': 200, 'content_type': 'application/pdf', 'text': 'PDF viewer'}

    def download(target: str, *, mime_type: str) -> dict[str, object]:
        assert target == url and mime_type == 'application/pdf'
        return {'status': 'ok', 'url': url, 'content_kind': 'pdf', 'bytes': b'%PDF-fixture', 'title': 'Source'}

    monkeypatch.setattr(browser, 'navigate', navigate)
    monkeypatch.setattr(browser, 'capture', capture)
    monkeypatch.setattr(source_download, '_download_pdf_streaming', download)
    try:
        page = TaskBrowserSource(browser).fetch(url)
        trace = acquisition_observation(page['acquisition'])
        attempts = trace['attempts']
        assert isinstance(attempts, list)
        assert [row['backend'] for row in attempts if isinstance(row, dict)] == ['browser', 'http']
        assert page['bytes'] == b'%PDF-fixture' and page['content_kind'] == 'pdf'
    finally:
        browser.close()
