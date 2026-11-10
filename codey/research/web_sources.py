"""Shared browser search and HTTP-first document acquisition for Chat and Research."""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from codey.research.source_acquisition import combine_acquisitions, with_attempt
from codey.research.source_download import fetch_http_document, finish_pdf_download
from codey.runtime.core import cancellation
from codey.types.json_value import JsonObject


@dataclass(frozen=True)
class BrowserSessionConfig:
    """Explicit browser-session policy for HTTP fallback rendering."""

    cdp_port: int = 0
    profile_dir: Path | None = None
    require_existing: bool = False
    bring_to_front: bool = False


class BrowserSource(Protocol):
    last_search_errors: list[dict[str, str]]

    def search(self, query: str, *, limit: int) -> list[JsonObject]: ...
    def fetch(self, url: str) -> dict[str, object]: ...
    def close(self) -> None: ...


def _background_browser(config: BrowserSessionConfig | None = None) -> BrowserSource:
    from codey.research.browser_search import (
        RESEARCH_CDP_PORT,
        RESEARCH_PROFILE,
        BrowserSearchProvider,
    )

    if config is None:
        return BrowserSearchProvider(headless=False, isolated=False)
    return BrowserSearchProvider(
        headless=False,
        isolated=False,
        cdp_port=config.cdp_port or RESEARCH_CDP_PORT,
        profile_dir=config.profile_dir or RESEARCH_PROFILE,
        require_existing_session=config.require_existing,
        bring_to_front=config.bring_to_front,
    )


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

    def __init__(
        self,
        *,
        browser_factory: Callable[[], BrowserSource] = _background_browser,
        browser_session: BrowserSessionConfig | None = None,
    ) -> None:
        self._browser_factory = browser_factory
        self.browser_session = browser_session
        self._browser: BrowserSource | None = None

    def _browser_source(self) -> BrowserSource:
        if self._browser is None:
            if self.browser_session is not None and self._browser_factory is _background_browser:
                self._browser = _background_browser(self.browser_session)
            else:
                self._browser = self._browser_factory()
        return self._browser

    @property
    def browser_source(self) -> BrowserSource:
        """Share the acquisition browser with task-owned interaction resources."""
        return self._browser_source()

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
