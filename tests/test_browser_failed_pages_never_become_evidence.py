"""Exercise acquisition and the real evidence owner with blank/challenge pages."""

from __future__ import annotations

import io
from types import SimpleNamespace
from unittest import mock

import pytest

from codey.research import browser_search, extract, source_download
from codey.research.ledger import ResearchLedger
from codey.research.source_gateway import ResearchSourceGateway

URL = "https://example.com/article"


def _retain_fetch_page(provider: browser_search.BrowserSearchProvider, page: mock.Mock, *, owned: bool) -> None:
    provider._fetch_page = page
    if owned:
        provider._register_owned_page_on_browser_thread(page)
    else:
        provider._register_borrowed_page_on_browser_thread(page)


@pytest.mark.parametrize("transport", ["http", "browser", "borrowed_browser"])
@pytest.mark.parametrize("html,failure", [
    ("<html><title>Blank</title><body></body></html>", "page_blank"),
    ("<html><title>pmc.ncbi.nlm.nih.gov</title><body><footer>HHS Vulnerability Disclosure</footer></body></html>",
     "page_blank"),
    ('<html><body>Show glossary<div aria-hidden="true"><p>Clinical trial glossary definitions.</p>'
     '<div style="display:none">' + "Unrelated definitions. " * 100 + '</div></div>'
     '<app-root></app-root></body></html>', "page_blank"),
    ("<html><body>Checking your browser before accessing this article</body></html>", "page_unavailable"),
    ("<html><body>ERROR: sample diagnostic explained by this documentation article.</body></html>", ""),
    ("<html><body>HHS Vulnerability Disclosure explains how to report a security issue.</body></html>", ""),
])
def test_failed_acquisition_is_error_and_never_records_evidence(monkeypatch, transport, html, failure):
    response = io.BytesIO(html.encode("utf-8"))
    response.status = 200
    response.headers = {"content-type": "text/html"}
    response.url = URL
    monkeypatch.setattr(source_download, "_open_url_no_redirect", lambda *args, **kwargs: response)
    monkeypatch.setattr(browser_search, "check_fetch_url", lambda *args, **kwargs: "")
    monkeypatch.setattr("codey.research.source_gateway.check_fetch_url", lambda *args, **kwargs: "")
    provider = browser_search.BrowserSearchProvider()
    page = mock.Mock(url=URL)
    page.goto.return_value = SimpleNamespace(status=200, headers={"content-type": "text/html"})
    if transport != "http":
        _retain_fetch_page(provider, page, owned=transport == "browser")
        monkeypatch.setattr(provider, "_ensure_fetch_page_on_browser_thread", lambda _: page)
        page.content.return_value = html
        monkeypatch.setattr(browser_search, "_FETCH_SETTLE_TIMEOUT_SECONDS", 0)
    fetch = (source_download._download_text_fallback if transport == "http"
             else provider._fetch_on_browser_thread)
    ledger = ResearchLedger()
    gateway = ResearchSourceGateway(SimpleNamespace(fetch=fetch), ledger)
    result = gateway.open(URL)
    if failure:
        assert result.status == "error"
        assert failure in result.detail
        assert result.document is None
        assert ledger.final_url_set() == set()
    else:
        assert result.status == "ok"
        assert "explained by this documentation" in result.document.text or "explains how" in result.document.text
        assert ledger.final_url_set() == {URL}
    if transport == "http":
        assert response.closed
    if transport != "http" and failure:
        if transport == "browser":
            page.close.assert_called_once_with()
        else:
            page.close.assert_not_called()
        page.unroute_all.assert_called_once_with(behavior="wait")
        assert provider._fetch_page is None
        assert not provider._owned_pages and not provider._borrowed_pages
    elif transport != "http":
        page.close.assert_not_called()
        assert provider._fetch_page is page


@pytest.mark.parametrize("transport", ["http", "browser", "borrowed_browser"])
@pytest.mark.parametrize("status", [404, 403, 429, 500, 503])
def test_http_error_body_cannot_become_a_citable_source(monkeypatch, transport, status):
    html = "<html><title>Error</title><body>A detailed error response with links and text</body></html>"
    response = io.BytesIO(html.encode())
    response.status = status
    response.headers = {"content-type": "text/html"}
    response.url = URL
    monkeypatch.setattr(source_download, "_open_url_no_redirect", lambda *a, **k: response)
    monkeypatch.setattr(browser_search, "check_fetch_url", lambda *a, **k: "")
    monkeypatch.setattr("codey.research.source_gateway.check_fetch_url", lambda *a, **k: "")
    provider = browser_search.BrowserSearchProvider()
    page = mock.Mock(url=URL)
    page.goto.return_value = SimpleNamespace(status=status, headers=response.headers)
    _retain_fetch_page(provider, page, owned=transport == "browser")
    monkeypatch.setattr(provider, "_ensure_fetch_page_on_browser_thread", lambda _: page)
    fetch = source_download._download_text_fallback if transport == "http" else provider._fetch_on_browser_thread
    ledger = ResearchLedger()
    result = ResearchSourceGateway(SimpleNamespace(fetch=fetch), ledger).open(URL)
    assert result.status == "error"
    assert f"HTTP {status}" in result.detail
    assert result.document is None
    assert not ledger.final_url_set()
    if transport == "http":
        assert response.closed
    else:
        page.content.assert_not_called()
        if transport == "browser":
            page.close.assert_called_once_with()
        else:
            page.close.assert_not_called()
        page.unroute_all.assert_called_once_with(behavior="wait")
        assert provider._fetch_page is None
        assert not provider._owned_pages and not provider._borrowed_pages
        assert page.remove_listener.call_count == 3


@pytest.mark.parametrize("owned", [True, False])
def test_denied_browser_redirect_preserves_policy_failure(monkeypatch: pytest.MonkeyPatch, owned: bool) -> None:
    denied_url = "http://127.0.0.1/private"
    page = mock.Mock(url=denied_url)
    page.goto.return_value = SimpleNamespace(status=200, headers={"content-type": "text/html"})
    provider = browser_search.BrowserSearchProvider()
    _retain_fetch_page(provider, page, owned=owned)
    monkeypatch.setattr(provider, "_prepare_fetch_page_on_browser_thread", lambda url: page)
    monkeypatch.setattr(browser_search, "check_fetch_url", lambda url: "private target denied" if url == denied_url else "")

    result = provider._fetch_on_browser_thread(URL)

    assert result["status"] == "error"
    assert result["failure_code"] == "policy_denied"
    assert result["delivery"] == "confirmed"
    page.content.assert_not_called()
    if owned:
        page.close.assert_called_once_with()
    else:
        page.close.assert_not_called()
    page.unroute_all.assert_called_once_with(behavior="wait")
    assert provider._fetch_page is None
    assert not provider._owned_pages and not provider._borrowed_pages


@pytest.mark.parametrize("url,content_type,status,expected", [
    (URL, "application/pdf", 503, "error"),
    (URL + ".pdf", "text/html", 404, "error"),
    (URL, "application/octet-stream", 200, "skipped"),
    (URL, "application/pdf", 200, "ok"),
    (URL + ".pdf", "application/octet-stream", 200, "ok"),
])
def test_response_headers_decide_failure_or_pdf_before_reading_dom(
    monkeypatch: pytest.MonkeyPatch, url: str, content_type: str, status: int, expected: str,
) -> None:
    provider = browser_search.BrowserSearchProvider()
    page = mock.Mock(url=url)
    page.goto.return_value = SimpleNamespace(status=status, headers={"content-type": content_type})
    _retain_fetch_page(provider, page, owned=True)
    monkeypatch.setattr(provider, "_prepare_fetch_page_on_browser_thread", lambda _: page)
    monkeypatch.setattr(browser_search, "check_fetch_url", lambda _: "")

    result = provider._fetch_on_browser_thread(url)

    assert result["status"] == expected
    page.content.assert_not_called()
    page.evaluate.assert_not_called()
    assert page.remove_listener.call_count == 3
    if status >= 400:
        assert result["http_status"] == status
        assert "content_kind" not in result
    elif expected == "ok":
        assert result["content_kind"] == "pdf_download"
    else:
        assert result["failure_code"] == "unsupported_content"
    if expected == "ok":
        page.close.assert_not_called()
        assert provider._fetch_page is page
    else:
        page.close.assert_called_once_with()
        assert provider._fetch_page is None


def test_navigation_text_waits_for_empty_application_main_to_hydrate(monkeypatch):
    shell = "<html><body><header>Navigation links and footer details.</header><app-root></app-root></body></html>"
    loaded = "<html><body><app-root><main>Actual trial population and status.</main></app-root></body></html>"
    page = mock.Mock()
    page.content.side_effect = [shell, loaded]
    page.evaluate.side_effect = [True, False]
    monkeypatch.setattr(browser_search.cancellation, "wait", lambda _: None)
    _, text = browser_search._fetch_page_content_after_settle(page)
    assert "Actual trial population" in text
    assert page.content.call_count == 2


def test_persistently_empty_application_main_cannot_become_navigation_evidence(monkeypatch):
    page = mock.Mock()
    page.content.return_value = "<header>Navigation has plenty of words.</header><app-root><main></main></app-root>"
    page.evaluate.return_value = True
    monkeypatch.setattr(browser_search, "_FETCH_SETTLE_TIMEOUT_SECONDS", 0)
    _, text = browser_search._fetch_page_content_after_settle(page)
    assert text == ""
    assert not extract._usable_fetch_page_text(text)
