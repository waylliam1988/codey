"""Real browser regressions for the shared form/search observation pipeline."""
from __future__ import annotations

import time
from urllib.parse import parse_qs, urlsplit

import pytest

from codey.automation.browser_contracts import RouteDecision
from codey.research.search_contracts import SearchState
from codey.research.task_browser_source import TaskBrowserSource
from codey.runtime.core.tool_resources import tool_resources_for
from tests.support.search_fixture import FORM, RESULT, SearchFixture
from tests.support.task_browser import browser_for


def test_result_shaped_links_outside_the_result_region_are_not_search_hits() -> None:
    def route(reply: RouteDecision) -> None:
        query = parse_qs(urlsplit(reply.request.url).query).get("q", [""])[0]
        from html import escape

        body = FORM.format(query=escape(query, quote=True))
        if query:
            body += "<ol id='b_results'></ol><aside>" + RESULT + "</aside>"
        reply.fulfill(content_type="text/html", body=body)
    browser = browser_for(route)
    try:
        outcome = TaskBrowserSource(browser, engine="bing").search("query", deadline=time.monotonic() + 5)
        assert outcome.state == SearchState.FAILED
        assert outcome.failure_kind == "search_page_blank"
    finally:
        browser.close()


def test_source_only_authorization_borrows_the_task_worker_without_exposing_actions() -> None:
    from codey.operations.kernel.kernel_context import KernelContext
    from codey.operations.kernel_contracts import KernelExecutionDeps, KernelRunRequest, KernelTransportDeps
    from codey.operations.provider_session import adapt_provider
    from codey.operations.task_session import TaskSession
    from codey.operations.turn_snapshot import build_turn_snapshot
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
            assert tool_resources_for(context.durable).task_browser(sink.run_id) is browser
            assert isinstance(source.browser_source, TaskBrowserSource) and source.browser_source.browser is browser
            assert "browser_click" not in build_turn_snapshot(session).inventory_names
    finally:
        browser.close()


@pytest.mark.parametrize("mode,state,kind", [
    ("hits", SearchState.HITS, ""),
    ("empty", SearchState.EMPTY, ""),
    ("shell", SearchState.FAILED, "search_page_blank"),
    ("structure", SearchState.FAILED, "search_structure_changed"),
    ("challenge", SearchState.FAILED, "search_challenge"),
    ("http_error", SearchState.FAILED, "search_page_unavailable"),
])
def test_real_form_search_classifies_only_observed_result_facts(mode: str, state: SearchState, kind: str) -> None:
    fixture = SearchFixture(mode)
    browser = browser_for(fixture.route)
    try:
        source = TaskBrowserSource(browser, engine="bing")
        outcome = source.search("frame identity", deadline=time.monotonic() + 5)
        assert outcome.state == state, outcome
        assert outcome.failure_kind == kind
        queries = [parse_qs(urlsplit(url).query) for url in fixture.requests if "/search?" in url]
        assert queries == [{"q": ["frame identity"], "form": ["fixture"]}]
        if outcome.hits:
            assert outcome.hits[0].url == "https://example.com/current"
        source.close()
        assert browser.snapshot().ok  # Borrowing source cannot close its owner's resource.
    finally:
        browser.close()


def test_delayed_result_after_old_2500ms_limit_and_warm_session_reuse() -> None:
    fixture = SearchFixture(delay_ms=2800)
    browser = browser_for(fixture.route)
    try:
        source = TaskBrowserSource(browser, engine="bing")
        first = source.search("first query", deadline=time.monotonic() + 10)
        target = browser.resource_binding()
        second = source.search("second query", deadline=time.monotonic() + 8)
        assert first.state == second.state == SearchState.HITS
        assert target == browser.resource_binding()
        assert fixture.requests.count("https://www.bing.com/") == 1
        assert second.attempts[0].requested_query == "second query"
    finally:
        browser.close()


def test_site_filter_empty_preserves_candidates_instead_of_claiming_blank_page() -> None:
    fixture = SearchFixture()
    browser = browser_for(fixture.route)
    try:
        outcome = TaskBrowserSource(browser, engine="bing").search("identity site:other.example", deadline=time.monotonic() + 6)
        assert outcome.state == SearchState.FILTERED_EMPTY
        assert outcome.attempts[0].rejected_urls == ("https://example.com/current",)
    finally:
        browser.close()


def test_binding_actions_keeps_the_existing_search_session_and_page_identity() -> None:
    from codey.operations.browser.browser_resources import bind_source_browser, source_browser
    from codey.research.tools import ResearchTools
    from codey.research.web_sources import WebSourceProvider
    fixture = SearchFixture()
    browser = browser_for(fixture.route, owner_task_id="r")
    renderer = TaskBrowserSource(browser, owns_browser=True)
    web = WebSourceProvider(browser_factory=lambda: renderer)
    try:
        assert web.search("warm session").state == SearchState.HITS
        binding = browser.resource_binding()
        tools = ResearchTools(web, None, None)
        assert source_browser(tools, owner_task_id="r") is browser
        bind_source_browser(tools, browser)
        assert web.browser_source is renderer
        assert browser.resource_binding() == binding
        web.close()
        assert browser.snapshot().ok
    finally:
        browser.close()
