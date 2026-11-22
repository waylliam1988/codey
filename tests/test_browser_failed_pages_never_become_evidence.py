"""Rejected sources stay observable without minting passages or closing borrowed tabs."""
from __future__ import annotations

import io
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from email.message import Message
from urllib.request import Request
from urllib.response import addinfourl

import pytest
from playwright.sync_api import Browser, Page, Response, Route, sync_playwright

from codey.research import browser_search, extract, source_download
from codey.research.ledger import ResearchLedger
from codey.research.source_gateway import ResearchSourceGateway
from codey.types.json_value import JsonObject
from tests.support.real_cdp import chromium_executable

URL = "https://example.com/article"


@pytest.fixture(scope="module")
def source_browser() -> Iterator[Browser]:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_executable(), headless=True)
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture()
def source_page(source_browser: Browser) -> Iterator[Page]:
    context = source_browser.new_context()
    try:
        yield context.new_page()
    finally:
        context.close()


@dataclass
class FetchSource:
    _fetch: Callable[[str], dict[str, object]]

    def fetch(self, url: str) -> dict[str, object]:
        return self._fetch(url)

    def search(self, query: str, *, limit: int) -> list[JsonObject]:
        return []


def _allow(url: str, *, use_cache: bool = False) -> str:
    return ""


def _retain_fetch_page(provider: browser_search.BrowserSearchProvider, page: Page, *, owned: bool) -> None:
    provider._fetch_page = page
    if owned:
        provider._register_owned_page_on_browser_thread(page)
    else:
        provider._register_borrowed_page_on_browser_thread(page)


def _prepare(monkeypatch: pytest.MonkeyPatch, provider: browser_search.BrowserSearchProvider, page: Page) -> None:
    def prepare(url: str) -> Page:
        return page

    monkeypatch.setattr(provider, "_prepare_fetch_page_on_browser_thread", prepare)
    monkeypatch.setattr(browser_search, "check_fetch_url", _allow)
    monkeypatch.setattr("codey.research.source_gateway.check_fetch_url", _allow)


def _http_response(monkeypatch: pytest.MonkeyPatch, html: str, status: int) -> io.BytesIO:
    stream = io.BytesIO(html.encode("utf-8"))
    headers = Message()
    headers["content-type"] = "text/html; charset=utf-8"
    response = addinfourl(stream, headers, URL, status)

    def open_response(request: Request, *, timeout: float) -> addinfourl:
        return response

    monkeypatch.setattr(source_download, "_open_url_no_redirect", open_response)
    return stream


def _assert_retained_then_release(provider: browser_search.BrowserSearchProvider, page: Page, *, owned: bool) -> None:
    retained = provider._fetch_page
    assert retained is page and not page.is_closed()
    assert id(page) in (provider._owned_pages if owned else provider._borrowed_pages)
    provider._discard_fetch_page_on_browser_thread(page)
    assert page.is_closed() is owned
    assert provider._fetch_page is None
    assert not provider._owned_pages and not provider._borrowed_pages


@pytest.mark.parametrize("transport", ["http", "browser", "borrowed_browser"])
@pytest.mark.parametrize("html,failure", [
    ("<html><title>Blank</title><body></body></html>", "page_blank"),
    ("<html><title>pmc.ncbi.nlm.nih.gov</title><body><footer>HHS Vulnerability Disclosure</footer></body></html>", "page_blank"),
    ('<html><body>Show glossary<div aria-hidden="true"><p>Clinical trial glossary definitions.</p>'
     '<div style="display:none">' + "Unrelated definitions. " * 100 + '</div></div>'
     '<app-root></app-root></body></html>', "page_blank"),
    ("<html><body>Checking your browser before accessing this article</body></html>", "page_unavailable"),
    ("<html><body>ERROR: sample diagnostic explained by this documentation article.</body></html>", ""),
    ("<html><body>HHS Vulnerability Disclosure explains how to report a security issue.</body></html>", ""),
])
def test_failed_acquisition_is_error_and_never_records_evidence(
    monkeypatch: pytest.MonkeyPatch, source_page: Page, transport: str, html: str, failure: str,
) -> None:
    stream = _http_response(monkeypatch, html, 200)
    provider = browser_search.BrowserSearchProvider()
    _prepare(monkeypatch, provider, source_page)
    monkeypatch.setattr(browser_search, "_FETCH_SETTLE_TIMEOUT_SECONDS", 0)

    def route(request: Route) -> None:
        request.fulfill(status=200, content_type="text/html", body=html)

    source_page.route("**/*", route)
    owned = transport == "browser"
    if transport != "http":
        _retain_fetch_page(provider, source_page, owned=owned)
    fetch = source_download._download_text_fallback if transport == "http" else provider._fetch_on_browser_thread
    ledger = ResearchLedger()
    result = ResearchSourceGateway(FetchSource(fetch), ledger).open(URL)
    if failure:
        expected_failure = "render_timeout" if "app-root" in html and transport != "http" else failure
        assert result.status == "error" and expected_failure in result.detail
        assert result.document is None and not ledger.final_url_set()
        assert not ledger.read_passages
    else:
        assert result.status == "ok" and result.document is not None
        assert "explained by this documentation" in result.document.text or "explains how" in result.document.text
        assert ledger.final_url_set() == {URL}
    if transport == "http":
        assert stream.closed
    else:
        _assert_retained_then_release(provider, source_page, owned=owned)


@pytest.mark.parametrize("transport", ["http", "browser", "borrowed_browser"])
@pytest.mark.parametrize("status", [404, 403, 429, 500, 503])
def test_http_error_body_cannot_become_a_citable_source(
    monkeypatch: pytest.MonkeyPatch, source_page: Page, transport: str, status: int,
) -> None:
    html = "<article>Readable-looking rejected response must not become evidence.</article>"
    stream = _http_response(monkeypatch, html, status)
    provider = browser_search.BrowserSearchProvider()
    _prepare(monkeypatch, provider, source_page)

    def route(request: Route) -> None:
        request.fulfill(status=status, content_type="text/html", body=html)

    source_page.route("**/*", route)
    owned = transport == "browser"
    if transport != "http":
        _retain_fetch_page(provider, source_page, owned=owned)
    fetch = source_download._download_text_fallback if transport == "http" else provider._fetch_on_browser_thread
    ledger = ResearchLedger()
    result = ResearchSourceGateway(FetchSource(fetch), ledger).open(URL)
    assert result.status == "error" and f"HTTP {status}" in result.detail
    assert result.document is None and not ledger.final_url_set() and not ledger.read_passages
    if transport == "http":
        assert stream.closed
    else:
        _assert_retained_then_release(provider, source_page, owned=owned)


@pytest.mark.parametrize("owned", [True, False])
def test_denied_browser_redirect_preserves_policy_failure(
    monkeypatch: pytest.MonkeyPatch, source_page: Page, owned: bool,
) -> None:
    denied_url = "https://example.com/private"
    provider = browser_search.BrowserSearchProvider()
    _retain_fetch_page(provider, source_page, owned=owned)
    _prepare(monkeypatch, provider, source_page)

    def check(url: str, *, use_cache: bool = False) -> str:
        return "private target denied" if url == denied_url else ""

    def route(request: Route) -> None:
        if request.request.url == URL:
            request.fulfill(status=302, headers={"location": denied_url})
        else:
            request.fulfill(status=200, content_type="text/html", body="<p>Private target</p>")

    source_page.route("**/*", route)
    monkeypatch.setattr(browser_search, "check_fetch_url", check)
    result = provider._fetch_on_browser_thread(URL)
    assert result["status"] == "error" and result["failure_code"] == "policy_denied"
    assert result["delivery"] == "confirmed" and source_page.is_closed() is owned
    assert provider._fetch_page is None and not provider._owned_pages and not provider._borrowed_pages


class HeaderResponse(Response):
    """Only status and headers are consumed before any page-body inspection."""

    def __init__(self, status: int, content_type: str) -> None:
        self._status = status
        self._headers = {"content-type": content_type}

    @property
    def status(self) -> int:
        return self._status

    @property
    def headers(self) -> dict[str, str]:
        return self._headers


@pytest.mark.parametrize("url,content_type,status,expected", [
    (URL, "application/pdf", 503, "error"), (URL + ".pdf", "text/html", 404, "error"),
    (URL, "application/octet-stream", 200, "skipped"), (URL, "application/pdf", 200, "ok"),
    (URL + ".pdf", "application/octet-stream", 200, "ok"),
])
def test_response_headers_decide_failure_or_pdf_before_reading_dom(
    monkeypatch: pytest.MonkeyPatch, source_page: Page, url: str, content_type: str, status: int, expected: str,
) -> None:
    def route(request: Route) -> None:
        request.fulfill(status=200, content_type="text/html", body="<p>Fixture page</p>")

    source_page.route("**/*", route)
    source_page.goto(url)
    provider = browser_search.BrowserSearchProvider()
    _retain_fetch_page(provider, source_page, owned=True)
    _prepare(monkeypatch, provider, source_page)
    inspected: list[str] = []

    def navigate(target: str, *, wait_until: str, timeout: float) -> Response:
        return HeaderResponse(status, content_type)

    def content() -> str:
        inspected.append("content")
        raise AssertionError("Response headers must decide before DOM inspection")

    def evaluate(expression: str) -> object:
        inspected.append("evaluate")
        raise AssertionError("Response headers must decide before DOM inspection")

    monkeypatch.setattr(source_page, "goto", navigate)
    monkeypatch.setattr(source_page, "content", content)
    monkeypatch.setattr(source_page, "evaluate", evaluate)
    result = provider._fetch_on_browser_thread(url)
    assert result["status"] == expected and not inspected
    if status >= 400:
        assert result["http_status"] == status and "content_kind" not in result
    elif expected == "ok":
        assert result["content_kind"] == "pdf_download"
    else:
        assert result["failure_code"] == "unsupported_content"
    _assert_retained_then_release(provider, source_page, owned=True)


def test_navigation_text_waits_for_empty_application_main_to_hydrate(
    monkeypatch: pytest.MonkeyPatch, source_page: Page,
) -> None:
    source_page.set_content('<header>Navigation links and footer details.</header><app-root></app-root>'
        '<script>setTimeout(()=>document.querySelector("app-root").innerHTML='
        '"<main>Actual trial population and status.</main>",150)</script>')
    monkeypatch.setattr(browser_search, "_FETCH_SETTLE_TIMEOUT_SECONDS", 2)
    _, text = browser_search._fetch_page_content_after_settle(source_page)
    assert "Actual trial population" in text


def test_persistently_empty_application_main_cannot_become_navigation_evidence(
    monkeypatch: pytest.MonkeyPatch, source_page: Page,
) -> None:
    source_page.set_content("<header>Navigation has plenty of words.</header><app-root><main></main></app-root>")
    monkeypatch.setattr(browser_search, "_FETCH_SETTLE_TIMEOUT_SECONDS", 0)
    _, text = browser_search._fetch_page_content_after_settle(source_page)
    assert text == "" and not extract._usable_fetch_page_text(text)
