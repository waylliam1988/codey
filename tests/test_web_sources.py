"""One acquisition policy serves both entries without backend-owned evidence."""
from __future__ import annotations

import io
import time
import urllib.error
from unittest.mock import Mock

import pytest

from codey.research import source_download
from codey.research.connector_search import ConnectorAwareSearchProvider
from codey.research.ledger import ResearchLedger
from codey.research.search_factory import default_research_search_provider
from codey.research.source_acquisition import acquisition_observation, with_attempt
from codey.research.source_gateway import ResearchSourceGateway
from codey.research.web_sources import WebSourceProvider
from codey.runtime.core import cancellation
from codey.types.json_value import JsonObject, decode_json_object
from tests.test_chat_web_provider import Browser

URL = "https://example.com/article"
BODY = "Original field record: the measured quantity is exactly 17 grams."


def backends(page: dict[str, object]) -> list[str]:
    entries = acquisition_observation(page["acquisition"])["attempts"]
    assert isinstance(entries, list)
    return [str(decode_json_object(row)["backend"]) for row in entries]


@pytest.mark.parametrize("http_status,code,render", [
    (200, "page_not_loaded", True),
    (200, "page_unavailable", True),
    (401, "access_restricted", True),
    (403, "access_restricted", True),
    (404, "page_unavailable", False),
    (410, "page_unavailable", False),
    (429, "access_restricted", False),
    (503, "page_unavailable", True),
    (None, "request_failed", True),
    (None, "policy_denied", False),
    (None, "unsupported_content", False),
])
def test_http_facts_choose_one_browser_attempt(
    monkeypatch: pytest.MonkeyPatch, http_status: int | None, code: str, render: bool,
) -> None:
    calls: list[str] = []
    browser = Browser()
    result: dict[str, object] = {"status": "error", "failure_code": code,
        "http_status": http_status, "detail": "Misleading diagnostic: HTTP 403 / 404", "url": URL}

    def http(url: str) -> dict[str, object]:
        calls.append(url)
        return with_attempt(result, "http", url)

    monkeypatch.setattr("codey.research.web_sources.fetch_http_document", http)
    provider = WebSourceProvider(browser_factory=lambda: browser)
    page = provider.fetch(URL)
    assert calls == [URL]
    assert browser.fetches == ([URL] if render else [])
    assert backends(page) == (["http", "browser"] if render else ["http"])
    assert page["status"] == ("ok" if render else "error")
    assert provider._browser is browser if render else provider._browser is None
    provider.close()


@pytest.mark.parametrize("http_status", [403, 404, 410, 429, 503])
@pytest.mark.parametrize("raised", [False, True])
def test_http_status_survives_both_response_paths(
    monkeypatch: pytest.MonkeyPatch, http_status: int, raised: bool,
) -> None:
    monkeypatch.setattr(source_download, "check_fetch_url", lambda *args, **kwargs: "")
    response = io.BytesIO(b"<body>Detailed error response is never article evidence.</body>")
    response.status = http_status
    response.headers = {"content-type": "text/html"}
    response.url = URL
    if raised:
        error = urllib.error.HTTPError(URL, http_status, "HTTP failure", None, response)
        monkeypatch.setattr(source_download, "_open_url_no_redirect", Mock(side_effect=error))
    else:
        monkeypatch.setattr(source_download, "_open_url_no_redirect", lambda *args, **kwargs: response)
    page = source_download.fetch_http_document(URL)
    assert page["http_status"] == http_status and page["status"] == "error"
    assert response.closed
    assert backends(page) == ["http"]
    assert "http_status" not in str(page["acquisition"])


@pytest.mark.parametrize("success", [True, False])
def test_gateway_accepts_only_final_capture_and_failures_never_become_passages(
    monkeypatch: pytest.MonkeyPatch, success: bool,
) -> None:
    browser = Browser()
    failure = source_download._fetch_failure(URL, "HTTP 403", code="access_restricted", http_status=403)
    monkeypatch.setattr("codey.research.web_sources.fetch_http_document", lambda url: with_attempt(failure, "http", url))
    capture = {"status": "ok", "url": URL, "text": BODY} if success else failure
    monkeypatch.setattr(browser, "fetch", lambda url: capture)
    monkeypatch.setattr("codey.research.source_gateway.check_fetch_url", lambda *args, **kwargs: "")
    ledger = ResearchLedger()
    gateway = ResearchSourceGateway(WebSourceProvider(browser_factory=lambda: browser), ledger)
    record = Mock(wraps=ledger.record_open_document)
    monkeypatch.setattr(ledger, "record_open_document", record)
    opened = gateway.open(URL)
    assert opened.status == ("ok" if success else "error")
    assert record.call_count == (1 if success else 0)
    assert len(ledger.opened_sources) == (1 if success else 0)
    assert not ledger.read_passages
    if opened.document is not None:
        assert opened.document.text == BODY
    assert backends({"acquisition": opened.acquisition}) == ["http", "browser"]


def test_browser_pdf_discovery_uses_one_downloader_and_preserves_all_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    browser = Browser()
    restricted = source_download._fetch_failure(URL, "HTTP 403", code="access_restricted", http_status=403)
    monkeypatch.setattr("codey.research.web_sources.fetch_http_document", lambda url: with_attempt(restricted, "http", url))
    monkeypatch.setattr(browser, "fetch", lambda url: source_download._pdf_download_sentinel(URL))
    download = Mock(return_value={"status": "ok", "url": URL, "content_kind": "pdf", "bytes": b"%PDF fixture", "text": ""})
    monkeypatch.setattr(source_download, "_download_pdf_streaming", download)
    page = WebSourceProvider(browser_factory=lambda: browser).fetch(URL)
    assert page["bytes"] == b"%PDF fixture" and page["content_kind"] == "pdf"
    assert backends(page) == ["http", "browser", "http"]
    entries = decode_json_object(page["acquisition"])["attempts"]
    assert isinstance(entries, list)
    assert [decode_json_object(entry)["observation"] for entry in entries] == ["restricted", "download", "document"]
    download.assert_called_once_with(URL, mime_type="application/pdf")


def test_direct_pdf_does_not_repeat_html_probe_or_start_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "https://example.com/article.pdf"
    download = Mock(return_value={"status": "ok", "url": url, "content_kind": "pdf", "bytes": b"%PDF fixture", "text": ""})
    monkeypatch.setattr(source_download, "_download_pdf_streaming", download)
    monkeypatch.setattr(source_download, "_download_text_fallback", Mock(side_effect=AssertionError("duplicate HTML probe")))
    browser = Mock(side_effect=AssertionError("PDF started browser"))
    page = WebSourceProvider(browser_factory=browser).fetch(url)
    assert page["content_kind"] == "pdf" and backends(page) == ["http"]
    download.assert_called_once_with(url)


@pytest.mark.parametrize("error", [cancellation.TaskCancelled, cancellation.DeadlineExceeded])
@pytest.mark.parametrize("stage", ["http", "browser"])
def test_stop_signals_propagate_without_another_backend(
    monkeypatch: pytest.MonkeyPatch, error: type[Exception], stage: str,
) -> None:
    browser = Browser()
    http_result = with_attempt(source_download._fetch_failure(URL, "empty", code="page_not_loaded"), "http", URL)
    http = Mock(side_effect=error()) if stage == "http" else Mock(return_value=http_result)
    render = Mock(side_effect=error())
    monkeypatch.setattr("codey.research.web_sources.fetch_http_document", http)
    monkeypatch.setattr(browser, "fetch", render)
    with pytest.raises(error):
        WebSourceProvider(browser_factory=lambda: browser).fetch(URL)
    assert http.call_count == 1 and render.call_count == (1 if stage == "browser" else 0)


def test_expired_total_deadline_does_not_start_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("codey.research.web_sources.fetch_http_document", lambda url:
        with_attempt(source_download._fetch_failure(url, "empty", code="page_not_loaded"), "http", url))
    factory = Mock(side_effect=AssertionError("expired task started browser"))
    with cancellation.deadline_scope(time.monotonic() - 1), pytest.raises(cancellation.DeadlineExceeded):
        WebSourceProvider(browser_factory=factory).fetch(URL)
    factory.assert_not_called()


def test_research_factory_reuses_shared_acquisition_and_preserves_search_diagnostics(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = default_research_search_provider()
    assert isinstance(provider.base_provider, WebSourceProvider)
    assert provider.base_provider._browser is None
    browser = Browser()
    browser.last_search_errors = [{"engine": "bing", "failure_kind": "search_challenge", "detail": "verification"}]
    shared = WebSourceProvider(browser_factory=lambda: browser)

    def fail(query: str, *, limit: int) -> list[JsonObject]:
        raise RuntimeError("search verification")

    monkeypatch.setattr(browser, "search", fail)
    research = ConnectorAwareSearchProvider(shared, connector_ids=(), rate_limit=False)
    with pytest.raises(RuntimeError, match="verification"):
        research.search("field record")
    assert research.last_search_failure == browser.last_search_errors[0]
    assert shared.last_search_errors is browser.last_search_errors
    research.close()
    assert browser.closed == 1
