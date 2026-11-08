"""Real browser search and bounded HTTP page acquisition for ordinary chat."""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Protocol

from codey.research.source_download import fetch_http_document
from codey.runtime.core import cancellation
from codey.types.json_value import JsonObject, JsonValue


class BrowserSource(Protocol):
    def search(self, query: str, *, limit: int) -> list[JsonObject]: ...
    def fetch(self, url: str) -> dict[str, object]: ...
    def close(self) -> None: ...


def _background_browser() -> BrowserSource:
    from codey.research.browser_search import BrowserSearchProvider

    return BrowserSearchProvider(headless=False, isolated=False, http_fallback=False)


def _attempt(backend: str, url: str, page: dict[str, object]) -> JsonObject:
    attempt: JsonObject = {"backend": backend, "requested_url": url,
                           "final_url": str(page.get("url") or url), "status": str(page.get("status") or "error")}
    if page.get("detail"):
        attempt["detail"] = str(page["detail"])
    return attempt


class ChatWebProvider:
    name = "browser_http"

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

    def fetch(self, url: str) -> dict[str, object]:
        with cancellation.deadline_scope(time.monotonic() + 30.0):
            page = fetch_http_document(url)
            cancellation.check()
            attempts: list[JsonValue] = [_attempt("http", url, page)]
            if page.get("render_required") is True:
                page = self._browser_source().fetch(url)
                cancellation.check()
                attempts.append(_attempt("browser", url, page))
            return {**page, "acquisition": {"version": 1, "attempts": attempts}}

    def close(self) -> None:
        if self._browser is not None:
            self._browser.close()
            self._browser = None
