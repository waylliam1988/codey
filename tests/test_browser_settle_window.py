"""Production settle behavior over real DOM hydration, deadlines and cancellation."""
from __future__ import annotations

import threading
import time
from collections.abc import Iterator

import pytest

from codey.automation.browser_contracts import RouteDecision
from codey.research.ledger import ResearchLedger
from codey.research.source_gateway import ResearchSourceGateway
from codey.research.task_browser_source import TaskBrowserSource
from codey.runtime.core import cancellation
from tests.support.task_browser import browser_for

URL = "https://example.com/settle/"
BODY = "Winter sample: 327 grams. Packaging is excluded."
SHELL = "<html><body><app-root><main>Loading...</main></app-root>{}</body></html>"
DOCUMENTS = {
    URL + "slow": SHELL.format("<script>setTimeout(() => document.querySelector('main').textContent = "
                               f"'{BODY}', 5500)</script>"),
    URL + "pending": SHELL.format(""),
    URL + "http-error": SHELL.format(""),
    URL + "ready": f"<html><body><article>{BODY}</article></body></html>",
}


@pytest.fixture
def source(public_source_dns: None) -> Iterator[tuple[TaskBrowserSource, list[str]]]:
    navigations: list[str] = []
    def route_document(route: RouteDecision) -> None:
        navigations.append(route.request.url)
        document = DOCUMENTS[route.request.url]
        status = 503 if route.request.url == URL + "http-error" else 200
        route.fulfill(status=status, content_type="text/html; charset=utf-8", body=document)
    browser = browser_for(route_document)
    try:
        yield TaskBrowserSource(browser), navigations
    finally:
        browser.close()


def test_slow_hydration_is_captured_after_old_four_second_limit(
    source: tuple[TaskBrowserSource, list[str]],
) -> None:
    provider, navigations = source
    ledger = ResearchLedger()
    opened = ResearchSourceGateway(provider, ledger).open(URL + "slow")
    assert opened.status == "ok", opened.detail
    assert opened.document is not None and BODY in opened.document.text
    assert ledger.final_url_set() == {URL + "slow"}
    assert navigations == [URL + "slow"]


def test_readable_page_returns_within_shorter_caller_deadline(
    source: tuple[TaskBrowserSource, list[str]],
) -> None:
    provider, _navigations = source
    assert provider.fetch(URL + "ready")["status"] == "ok"  # Warm browser outside the caller budget.
    with cancellation.deadline_scope(time.monotonic() + 2.0):
        fetched = provider.fetch(URL + "ready")
    assert fetched["status"] == "ok" and BODY in str(fetched["text"])


def test_persistently_loading_page_expires_without_source_evidence(
    source: tuple[TaskBrowserSource, list[str]],
) -> None:
    provider, navigations = source
    ledger = ResearchLedger()
    opened = ResearchSourceGateway(provider, ledger).open(URL + "pending")
    assert opened.status == "error" and opened.failure_code == "page_not_loaded"
    assert "render_timeout" in opened.detail
    assert opened.document is None and not ledger.final_url_set()
    assert navigations == [URL + "pending"]


def test_http_error_rejects_loading_shell_before_dom_read(
    source: tuple[TaskBrowserSource, list[str]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, navigations = source
    ledger = ResearchLedger()
    def forbidden_capture() -> dict[str, object]:
        raise AssertionError("HTTP error must be rejected before reading the DOM")
    monkeypatch.setattr(provider.browser, "capture", forbidden_capture)

    opened = ResearchSourceGateway(provider, ledger).open(URL + "http-error")

    assert opened.status == "error" and "HTTP 503" in opened.detail
    assert opened.document is None and not ledger.final_url_set()
    assert navigations == [URL + "http-error"]


def test_caller_deadline_interrupts_loading_before_settle_limit(
    source: tuple[TaskBrowserSource, list[str]],
) -> None:
    provider, _navigations = source
    assert provider.fetch(URL + "ready")["status"] == "ok"
    ledger = ResearchLedger()
    with cancellation.deadline_scope(time.monotonic() + 1.0), pytest.raises(cancellation.DeadlineExceeded):
        ResearchSourceGateway(provider, ledger).open(URL + "pending")
    assert not ledger.final_url_set()


def test_cancellation_interrupts_loading_without_source_evidence(
    source: tuple[TaskBrowserSource, list[str]],
) -> None:
    provider, _navigations = source
    assert provider.fetch(URL + "ready")["status"] == "ok"
    ledger = ResearchLedger()
    stop = threading.Event()
    timer = threading.Timer(1.0, stop.set)
    timer.start()
    try:
        with cancellation.scope(stop), pytest.raises(cancellation.TaskCancelled):
            ResearchSourceGateway(provider, ledger).open(URL + "pending")
    finally:
        timer.cancel()
        timer.join()
    assert not ledger.final_url_set()
