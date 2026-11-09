"""Real Chromium checks for same-page user-assisted browser resume."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import cast

import pytest
from playwright.sync_api import Page

from codey.research import browser_search
from tests.support.real_cdp import chromium_executable
from tools.browser_experiment_sources import ResumableBrowserSearchProvider


class _ChallengeHandler(BaseHTTPRequestHandler):
    requests = 0
    body = b"<html><body>Verify you are human</body></html>"

    def do_GET(self) -> None:  # noqa: N802
        type(self).requests += 1
        body = type(self).body
        self.send_response(403)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.mark.parametrize("body", [
    b"<html><body>Verify you are human</body></html>",
    '{"error":{"message":"您当前请求存在异常，暂时限制本次访问。","code":40362}}'.encode(),
])
def test_research_provider_retains_challenge_page_and_resumes_in_place(
    monkeypatch: pytest.MonkeyPatch,
    body: bytes,
) -> None:
    monkeypatch.setattr(_ChallengeHandler, "body", body)
    server = HTTPServer(("127.0.0.1", 0), _ChallengeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _ChallengeHandler.requests = 0
    provider = ResumableBrowserSearchProvider(
        headless=True,
        browser_path=chromium_executable(),
    )
    def allow_fetch(_url: str, *, use_cache: bool = False) -> str:
        return ""

    monkeypatch.setattr(browser_search, "check_fetch_url", allow_fetch)
    url = f"http://127.0.0.1:{server.server_port}/challenge"
    try:
        failed = provider.fetch(url)
        assert failed["status"] == "error"
        unresolved = provider.resume_fetch()
        assert unresolved["status"] == "error"
        assert unresolved["failure_code"] == "page_unavailable"
        page_id = id(cast(Page, provider._fetch_page))

        def user_resolves_challenge() -> int:
            page = provider._fetch_page
            assert page is not None
            page.set_content("<html><body><article>Visible article after verification.</article></body></html>")
            return id(page)

        assert browser_search._search_browser_call(user_resolves_challenge) == page_id
        resumed = provider.resume_fetch()
        assert resumed["status"] == "ok"
        assert "Visible article after verification" in str(resumed["text"])
        assert id(cast(Page, provider._fetch_page)) == page_id
        assert _ChallengeHandler.requests == 1
    finally:
        provider.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_resumable_provider_reports_missing_page_after_close() -> None:
    provider = ResumableBrowserSearchProvider(headless=True,
                                              browser_path=chromium_executable())
    try:
        provider.close()
        result = provider.resume_fetch()
        assert result["status"] == "error"
        assert result["failure_code"] == "snapshot_unavailable"
    finally:
        provider.close()
