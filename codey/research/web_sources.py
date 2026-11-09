"""Shared browser search and HTTP-first document acquisition for Chat and Research."""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Protocol

from codey.research.source_acquisition import combine_acquisitions, with_attempt
from codey.research.source_download import fetch_http_document, finish_pdf_download
from codey.runtime.core import cancellation
from codey.types.json_value import JsonObject


class BrowserSource(Protocol):
    last_search_errors: list[dict[str, str]]

    def search(self, query: str, *, limit: int) -> list[JsonObject]: ...
    def fetch(self, url: str) -> dict[str, object]: ...
    def close(self) -> None: ...


def _background_browser() -> BrowserSource:
    from codey.research.browser_search import BrowserSearchProvider

    return BrowserSearchProvider(headless=False, isolated=False)


def _should_try_browser(page: dict[str, object]) -> bool:
    """Choose one rendering attempt using transport facts, never diagnostic prose."""
    if page.get("status") != "error" or page.get("content_kind") == "pdf":
        return False
    code = page.get("failure_code")
    status = page.get("http_status")
    if code == "policy_denied" or status in (404, 410, 429):
        return False
    if isinstance(status, int) and status >= 400:
        return status in (401, 403) or status >= 500
    return code in {"page_not_loaded", "access_restricted", "login_required", "request_failed"} or (
        code == "page_unavailable" and status == 200
    )


class WebSourceProvider:
    name = "http_browser"

    def __init__(self, *, browser_factory: Callable[[], BrowserSource] = _background_browser) -> None:
        self._browser_factory = browser_factory
        self._browser: BrowserSource | None = None

    def _browser_source(self) -> BrowserSource:
        if self._browser is None:
            self._browser = self._browser_factory()
        return self._browser

    def search(self, query: str, *, limit: int = 8) -> list[JsonObject]:
        cancellation.check()
        results = self._browser_source().search(query, limit=max(1, min(limit, 8)))
        cancellation.check()
        return results

    @property
    def last_search_errors(self) -> list[dict[str, str]]:
        return self._browser.last_search_errors if self._browser is not None else []

    def fetch(self, url: str) -> dict[str, object]:
        with cancellation.deadline_scope(time.monotonic() + 30.0):
            http_page = fetch_http_document(url)
            cancellation.check()
            if not _should_try_browser(http_page):
                return http_page
            browser_page = with_attempt(self._browser_source().fetch(url), "browser", url)
            cancellation.check()
            final_page = finish_pdf_download(browser_page, url)
            cancellation.check()
            return combine_acquisitions(http_page, final_page)

    def close(self) -> None:
        if self._browser is not None:
            self._browser.close()
            self._browser = None
