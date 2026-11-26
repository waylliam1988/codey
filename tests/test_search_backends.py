"""HTTP backend shape, fallback, budgets and diagnostic ownership."""
from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from codey.research.search_api import SearchApi
from codey.research.search_contracts import SearchOutcome, SearchState, failed_search
from codey.research.search_profiles import SearchProfile
from codey.research.web_sources import WebSourceProvider
from tests.test_chat_web_provider import Browser


@contextmanager
def api_server(payload: object, *, redirect: str = "", broken_chunks: bool = False) -> Iterator[tuple[str, list[str]]]:
    requests: list[str] = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append(self.path)
            body = json.dumps(payload).encode("utf-8")
            self.send_response(302 if redirect else 200)
            if redirect:
                self.send_header("Location", redirect)
            self.send_header("Content-Type", "application/json")
            if broken_chunks:
                self.send_header("Transfer-Encoding", "chunked")
            else:
                self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(b"invalid-chunk-size\r\n" if broken_chunks else body)
        def log_message(self, format: str, *args: object) -> None:
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/search", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("rows,state", [
    ([], SearchState.EMPTY),
    ([{"title": "Official", "url": "https://docs.python.org/3/library/pathlib.html", "content": "Observed"}], SearchState.HITS),
])
def test_configured_api_success_never_constructs_browser(rows: list[dict[str, str]], state: SearchState) -> None:
    def forbidden() -> Browser:
        raise AssertionError("API success must not start a browser")
    with api_server({"results": rows}) as (endpoint, requests):
        provider = WebSourceProvider(api=SearchApi(endpoint), browser_factory=forbidden)
        outcome = provider.search("Path + quoted terms")
        assert outcome.state == state
        assert provider._browser is None
        assert parse_qs(urlsplit(requests[0]).query)["q"] == ["Path + quoted terms"]


@pytest.mark.parametrize("payload", [
    {}, {"results": "not an array"}, {"results": [], "unresponsive_engines": [["bing", "timeout"]]},
    {"results": [{"title": "Broken", "url": None}]},
])
def test_invalid_or_failed_api_falls_back_without_false_empty(payload: object) -> None:
    browser = Browser()
    with api_server(payload) as (endpoint, _requests):
        provider = WebSourceProvider(api=SearchApi(endpoint), browser_factory=lambda: browser)
        outcome = provider.search("query")
        assert outcome.state == SearchState.HITS
        assert outcome.attempts[0].state == "failed"
        assert len(browser.searches) == 1


def test_api_redirect_never_contacts_its_destination() -> None:
    with api_server({"results": []}) as (destination, requests), api_server({}, redirect=destination) as (endpoint, _):
        outcome = SearchApi(endpoint).search("query", limit=3, timeout=2)
        assert outcome.state == SearchState.FAILED
        assert requests == []


def test_broken_http_framing_is_a_backend_failure_and_falls_back() -> None:
    browser = Browser()
    with api_server({}, broken_chunks=True) as (endpoint, requests):
        outcome = WebSourceProvider(api=SearchApi(endpoint), browser_factory=lambda: browser).search("query")
        assert len(requests) == 1
        assert outcome.state == SearchState.HITS
        assert outcome.attempts[0].failure_kind == "search_api_failed"
        assert len(browser.searches) == 1


def test_one_coordinator_retains_first_failure_and_next_engine_success() -> None:
    class RecoveringBrowser(Browser):
        def search(self, query: str, *, limit: int, profile: SearchProfile, deadline: float) -> SearchOutcome:
            assert deadline > time.monotonic()
            if profile.name == "bing":
                self.searches.append((query, limit))
                return failed_search(query, profile.name, "search_page_blank")
            return super().search(query, limit=limit, profile=profile, deadline=deadline)
    browser = RecoveringBrowser()
    provider = WebSourceProvider(browser_factory=lambda: browser)
    outcome = provider.search("same query")
    assert outcome.state == SearchState.HITS
    assert [attempt.engine for attempt in outcome.attempts] == ["bing", "duckduckgo"]
    assert provider.last_search_errors == outcome.errors
    assert len(browser.searches) == 2
