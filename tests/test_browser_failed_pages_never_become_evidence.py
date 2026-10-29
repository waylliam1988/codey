"""Exercise acquisition and the real evidence owner with blank/challenge pages."""

from __future__ import annotations

import io
from types import SimpleNamespace
from unittest import mock

import pytest

from codey.research import browser_search
from codey.research.ledger import ResearchLedger
from codey.research.source_gateway import ResearchSourceGateway

URL = "https://example.com/article"


@pytest.mark.parametrize("transport", ["http", "browser"])
@pytest.mark.parametrize("html,failure", [
    ("<html><title>Blank</title><body></body></html>", "page_blank"),
    ('<html><body>Show glossary<div aria-hidden="true"><p>Clinical trial glossary definitions.</p>'
     '<div style="display:none">' + "Unrelated definitions. " * 100 + '</div></div>'
     '<app-root></app-root></body></html>', "page_blank"),
    ("<html><body>Checking your browser before accessing this article</body></html>", "page_unavailable"),
    ("<html><body>ERROR: sample diagnostic explained by this documentation article.</body></html>", ""),
])
def test_failed_acquisition_is_error_and_never_records_evidence(monkeypatch, transport, html, failure):
    response = io.BytesIO(html.encode("utf-8"))
    response.status = 200
    response.headers = {"content-type": "text/html"}
    response.geturl = lambda: URL
    monkeypatch.setattr(browser_search, "_open_url_no_redirect", lambda *args, **kwargs: response)
    monkeypatch.setattr(browser_search, "check_fetch_url", lambda *args, **kwargs: "")
    monkeypatch.setattr("codey.research.source_gateway.check_fetch_url", lambda *args, **kwargs: "")
    provider = browser_search.BrowserSearchProvider()
    page = mock.Mock(url=URL)
    page.goto.return_value = SimpleNamespace(headers={"content-type": "text/html"})
    if transport == "browser":
        provider._fetch_page = page
        monkeypatch.setattr(provider, "_ensure_fetch_page_on_browser_thread", lambda _: page)
        monkeypatch.setattr(browser_search, "_fetch_page_content_after_settle", lambda _: (html, browser_search.extract_text(html)))
    fetch = (browser_search._download_text_fallback if transport == "http"
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
        assert result.document.text.startswith("ERROR: sample diagnostic")
        assert ledger.final_url_set() == {URL}
    if transport == "http" or failure:
        assert response.closed
    if transport == "browser" and failure:
        page.close.assert_called_once_with()
        assert provider._fetch_page is None
    elif transport == "browser":
        page.close.assert_not_called()
        assert provider._fetch_page is page


@pytest.mark.parametrize("transport", ["http", "browser"])
@pytest.mark.parametrize("status", [404, 403, 429, 500, 503])
def test_http_error_body_cannot_become_a_citable_source(monkeypatch, transport, status):
    html = "<html><title>Error</title><body>A detailed error response with links and text</body></html>"
    response = io.BytesIO(html.encode())
    response.status = status
    response.headers = {"content-type": "text/html"}
    response.geturl = lambda: URL
    monkeypatch.setattr(browser_search, "_open_url_no_redirect", lambda *a, **k: response)
    monkeypatch.setattr(browser_search, "check_fetch_url", lambda *a, **k: "")
    monkeypatch.setattr("codey.research.source_gateway.check_fetch_url", lambda *a, **k: "")
    provider = browser_search.BrowserSearchProvider()
    page = mock.Mock(url=URL)
    page.goto.return_value = SimpleNamespace(status=status, headers=response.headers)
    monkeypatch.setattr(provider, "_ensure_fetch_page_on_browser_thread", lambda _: page)
    fetch = browser_search._download_text_fallback if transport == "http" else provider._fetch_on_browser_thread
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
        page.close.assert_called_once_with()
