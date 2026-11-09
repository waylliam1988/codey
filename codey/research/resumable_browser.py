"""Research browser provider that retains a bound page for user-assisted resume."""

from __future__ import annotations

from typing import cast

from playwright.sync_api import Page

from codey.research import source_download
from codey.research.browser_search import (
    _FETCH_TOTAL_TIMEOUT_SECONDS,
    BrowserSearchProvider,
    _fetch_page_content_after_settle,
    _requires_login,
    _search_browser_call,
)
from codey.research.extract import _usable_fetch_page_text, extract_title
from codey.research.source_acquisition import with_attempt


class ResumableBrowserSearchProvider(BrowserSearchProvider):
    """Keep the task page after a bounded failure and read it again in place."""

    def _discard_fetch_page_on_browser_thread(self, page: Page) -> None:
        """Retain the bound page; task cancellation still closes the session."""
        if page is not self._fetch_page:
            super()._discard_fetch_page_on_browser_thread(page)

    def resume_fetch(self) -> dict[str, object]:
        """Read the current bound page without navigating or acquiring a URL again."""
        result = _search_browser_call(
            self._resume_fetch_on_browser_thread,
            timeout=_FETCH_TOTAL_TIMEOUT_SECONDS,
        )
        return with_attempt(result, "browser", str(result.get("url") or ""))

    def _resume_fetch_on_browser_thread(self) -> dict[str, object]:
        page = cast(Page | None, self._fetch_page)
        if page is None or page.is_closed():
            return source_download._fetch_failure(
                "",
                "the retained browser page is unavailable",
                code="snapshot_unavailable",
                delivery="not_attempted",
            )
        url = page.url
        html, text = _fetch_page_content_after_settle(page)
        if _requires_login(page, text):
            return source_download._fetch_failure(
                url,
                "login_required: page requires authentication",
                code="login_required",
                delivery="confirmed",
            )
        if not _usable_fetch_page_text(text):
            return source_download._fetch_failure(
                url,
                "page had no usable visible content after the current-page observation",
                code="page_not_loaded",
                delivery="confirmed",
            )
        truncated = len(text) > 200_000
        if truncated:
            text = text[:200_000]
        return {
            "status": "ok",
            "url": url,
            "title": extract_title(html),
            "text": text,
            "truncated": truncated,
            "observation": "current_page",
        }


__all__ = ["ResumableBrowserSearchProvider"]
