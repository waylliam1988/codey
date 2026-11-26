"""Failed captures remain observable without minting source-reading evidence."""
from __future__ import annotations

import io
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from email.message import Message
from urllib.request import Request
from urllib.response import addinfourl

import pytest

from codey.automation.browser_contracts import RouteDecision
from codey.research import source_download, task_browser_source
from codey.research.ledger import ResearchLedger
from codey.research.search_contracts import SearchOutcome
from codey.research.source_gateway import ResearchSourceGateway
from codey.research.task_browser_source import TaskBrowserSource
from tests.support.task_browser import browser_for

URL = "https://example.com/article"


@dataclass
class Fixture:
    html: str = ""
    status: int = 200
    def route(self, reply: RouteDecision) -> None:
        reply.fulfill(status=self.status, content_type="text/html", body=self.html)


@dataclass
class FetchSource:
    fetch: Callable[[str], dict[str, object]]
    def search(self, query: str, *, limit: int) -> SearchOutcome:
        raise AssertionError("source fixture cannot search")


@pytest.fixture(scope="module")
def source() -> Iterator[tuple[TaskBrowserSource, Fixture]]:
    fixture = Fixture()
    browser = browser_for(fixture.route)
    try:
        yield TaskBrowserSource(browser), fixture
    finally:
        browser.close()


def prepare_http(monkeypatch: pytest.MonkeyPatch, html: str, status: int) -> io.BytesIO:
    body = io.BytesIO(html.encode("utf-8"))
    headers = Message()
    headers["content-type"] = "text/html"
    response = addinfourl(body, headers, URL, status)
    def opened(request: Request, *, timeout: float) -> addinfourl:
        return response
    monkeypatch.setattr(source_download, "_open_url_no_redirect", opened)
    return body


@pytest.mark.parametrize("transport", ["http", "browser"])
@pytest.mark.parametrize("html,failed", [
    ("<body></body>", True),
    ("<body><footer>HHS Vulnerability Disclosure</footer></body>", True),
    ("<body>Show glossary<div aria-hidden='true'>" + "Definitions " * 100 + "</div><app-root></app-root></body>", True),
    ("<body>Checking your browser before accessing this article</body>", True),
    ("<body><input type='password'>Sign in to continue with your account</body>", True),
    ("<article>ERROR: sample diagnostic explained by this documentation article.</article>", False),
    ("<article>HHS Vulnerability Disclosure explains how to report a security issue.</article>", False),
])
def test_failed_acquisition_never_records_evidence(monkeypatch: pytest.MonkeyPatch,
        source: tuple[TaskBrowserSource, Fixture], transport: str, html: str, failed: bool, public_source_dns: None) -> None:
    browser_source, fixture = source
    fixture.html, fixture.status = html, 200
    stream = prepare_http(monkeypatch, html, 200)
    monkeypatch.setattr(task_browser_source, "_FETCH_SETTLE_TIMEOUT_SECONDS", 0.15)
    fetch = source_download._download_text_fallback if transport == "http" else browser_source.fetch
    ledger = ResearchLedger()
    result = ResearchSourceGateway(FetchSource(fetch), ledger).open(URL)
    assert (result.status == "error") is failed
    assert bool(ledger.opened_sources) is not failed
    assert not ledger.read_passages
    if transport == "http":
        assert stream.closed
    assert browser_source.browser.snapshot().ok if transport == "browser" else True


@pytest.mark.parametrize("transport", ["http", "browser"])
@pytest.mark.parametrize("status", [403, 404, 429, 500, 503])
def test_http_error_body_cannot_become_a_citable_source(monkeypatch: pytest.MonkeyPatch,
        source: tuple[TaskBrowserSource, Fixture], transport: str, status: int, public_source_dns: None) -> None:
    browser_source, fixture = source
    fixture.html = "<article>Readable looking rejected response is not source evidence.</article>"
    fixture.status = status
    prepare_http(monkeypatch, fixture.html, status)
    fetch = source_download._download_text_fallback if transport == "http" else browser_source.fetch
    ledger = ResearchLedger()
    result = ResearchSourceGateway(FetchSource(fetch), ledger).open(URL)
    assert result.status == "error" and result.document is None
    assert not ledger.opened_sources and not ledger.read_passages


def test_real_script_error_explains_unrendered_source(monkeypatch: pytest.MonkeyPatch,
        source: tuple[TaskBrowserSource, Fixture]) -> None:
    browser_source, fixture = source
    fixture.html = "<app-root></app-root><script>throw new Error('fixture hydration failed')</script>"
    fixture.status = 200
    monkeypatch.setattr(task_browser_source, "_FETCH_SETTLE_TIMEOUT_SECONDS", 0.15)
    page = browser_source.fetch(URL)
    assert page["status"] == "error" and page["failure_code"] == "render_script_error"
    browser_source.close()
    assert browser_source.browser.snapshot().ok


def test_real_failed_script_request_explains_unrendered_source(monkeypatch: pytest.MonkeyPatch) -> None:
    def route(reply: RouteDecision) -> None:
        if reply.request.url.endswith(".js"):
            reply.abort()
        else:
            reply.fulfill(content_type="text/html", body="<app-root></app-root><script src='/failed.js'></script>")
    browser = browser_for(route)
    monkeypatch.setattr(task_browser_source, "_FETCH_SETTLE_TIMEOUT_SECONDS", 0.15)
    try:
        page = TaskBrowserSource(browser).fetch(URL)
        assert page["status"] == "error" and page["failure_code"] == "render_request_failed"
    finally:
        browser.close()
