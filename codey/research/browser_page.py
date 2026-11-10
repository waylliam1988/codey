"""Shared acceptance of fetched and retained browser documents."""
from __future__ import annotations

import contextlib
import time

from playwright.sync_api import Error as BrowserError
from playwright.sync_api import Page, Request, Response

from codey.research import source_download
from codey.research.extract import _fetch_page_failure_kind, _usable_fetch_page_text, extract_title
from codey.research.source_acquisition import SourceFailureCode, http_failure_code
from codey.runtime.core import cancellation

_CONTENT_RETRY_TIMEOUT = 3.0
_CONTENT_RETRY_TICK = 0.2


class _PageFailures:
    """Bounded observations for one navigation; unrelated errors cannot veto usable text."""

    def __init__(self, page: Page) -> None:
        self.page = page
        self.errors: list[tuple[SourceFailureCode, str]] = []
        page.on("pageerror", self.script_error)
        page.on("requestfailed", self.request_failed)
        page.on("response", self.response_failed)

    def script_error(self, error: BrowserError) -> None:
        if len(self.errors) < 8:
            self.errors.append(("render_script_error", "render_script_error: " + error.message[:240]))

    def request_failed(self, request: Request) -> None:
        if request.resource_type in {"script", "xhr", "fetch"} and len(self.errors) < 8:
            self.errors.append(("render_request_failed", "render_request_failed: " + (request.failure or "unknown")[:240]))

    def response_failed(self, response: Response) -> None:
        if response.status >= 400 and response.request.resource_type in {"script", "xhr", "fetch"} and len(self.errors) < 8:
            self.errors.append(("render_request_failed", f"render_request_failed: HTTP {response.status}"))

    def failure(self, text: str) -> tuple[SourceFailureCode, str]:
        if self.errors:
            return self.errors[0][0], "; ".join(detail for _code, detail in self.errors)
        detail = "render_timeout" if _application_content_pending(self.page) else _fetch_page_failure_kind(text)
        return ("access_restricted" if detail == "page_unavailable" else "page_not_loaded"), detail

    def close(self) -> None:
        self.page.remove_listener("pageerror", self.script_error)
        self.page.remove_listener("requestfailed", self.request_failed)
        self.page.remove_listener("response", self.response_failed)


def _requires_login(page: Page, text: str) -> bool:
    return (len(text) < 1500 and any(marker in text.casefold() for marker in ("log in", "sign in", "登录", "登入"))
            and page.query_selector('input[type="password"]') is not None)


def _validate_page_status(url: str, response_status: int | None) -> dict[str, object] | None:
    """Reject failed responses before reading the DOM or selecting a PDF download."""
    if response_status is not None and response_status >= 400:
        return source_download._fetch_failure(
            url, f"could not load page: HTTP {response_status}",
            code=http_failure_code(response_status), delivery="confirmed", http_status=response_status,
        )
    return None


def _validate_page_content_type(url: str, content_type: str) -> dict[str, object] | None:
    normalized_type = content_type.lower()
    if normalized_type and not any(kind in normalized_type for kind in ("html", "text", "xml", "json")):
        return source_download._fetch_failure(
            url, f"unsupported content type: {normalized_type}", status="skipped",
            code="unsupported_content", delivery="confirmed",
        )
    return None


def _validate_page_observation(
    page: Page,
    url: str,
    html: str,
    text: str,
    *,
    failures: _PageFailures | None = None,
) -> dict[str, object] | None:
    """Validate visible content after response checks and DOM settling."""
    if _requires_login(page, text):
        return source_download._fetch_failure(
            url, "login_required: page requires authentication",
            code="login_required", delivery="confirmed",
        )
    if not _usable_fetch_page_text(text):
        if failures is None:
            detail = _fetch_page_failure_kind(text)
            code: SourceFailureCode = "access_restricted" if detail == "page_unavailable" else "page_not_loaded"
        else:
            code, detail = failures.failure(text)
        return source_download._fetch_failure(
            url, "page had no usable visible content after navigation: " + detail,
            title=extract_title(html), code=code, delivery="confirmed",
        )
    return None


def _application_content_pending(page: Page) -> bool:
    """Recognize an empty app/main region while the SPA is still hydrating."""
    try:
        pending: object = page.evaluate("""() => {
            const roots = [...document.querySelectorAll('app-root, #root, #app, #__next')];
            return roots.length > 0 && roots.every(root => {
                const main = root.querySelector('main, [role="main"]');
                const text = (main || root).innerText.trim();
                return !text || /^(loading|please wait|加载中)[.\\s…]*$/i.test(text);
            });
        }""")
        return pending is True
    except Exception:
        return False


def _page_content_after_navigation(page: Page) -> str:
    stop_at = time.monotonic() + _CONTENT_RETRY_TIMEOUT
    while True:
        cancellation.check()
        try:
            return str(page.content() or "")
        except Exception as exc:
            if not _content_retryable(exc) or time.monotonic() >= stop_at:
                raise
            with contextlib.suppress(Exception):
                page.wait_for_load_state("domcontentloaded", timeout=500)
            cancellation.wait(_CONTENT_RETRY_TICK)


def _content_retryable(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        "page is navigating" in text
        or "navigating and changing the content" in text
        or "execution context was destroyed" in text
    )
