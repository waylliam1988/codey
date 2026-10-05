"""Zero-key web search and page fetch for Research."""

from __future__ import annotations

import base64
import binascii
import contextlib
import json
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar, cast
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

from codey.automation import browser_worker
from codey.automation.browser import DEFAULT_PORT, open_chat_page
from codey.policies.network import check_fetch_url
from codey.research.extract import extract_text, extract_title
from codey.research.http_redirects import (
    build_no_redirect_opener,
    response_charset,
)
from codey.research.http_redirects import (
    close_response as _close_response,
)
from codey.research.http_redirects import (
    is_redirect_status as _is_redirect_status,
)
from codey.research.http_redirects import (
    redirect_target as _redirect_target,
)
from codey.research.pdf_extract import PDF_MAX_BYTES
from codey.runtime.core import cancellation
from codey.storage.local_store import DEFAULT_STATE_HOME

_PROFILES_PATH = Path(__file__).with_name("search_profiles.json")
RESEARCH_PROFILE = DEFAULT_STATE_HOME / "research-edge-profile"
RESEARCH_CDP_PORT = DEFAULT_PORT + 40
_NAV_TIMEOUT_MS = 20_000
_SEARCH_NAV_TIMEOUT_MS = 8_000
_SEARCH_NAV_ATTEMPTS = 2
_SEARCH_TOTAL_TIMEOUT_SECONDS = 24.0
_FETCH_NAV_TIMEOUT_MS = 10_000
_FETCH_TOTAL_TIMEOUT_SECONDS = 16.0
_FETCH_SETTLE_TIMEOUT_SECONDS = 4.0
_FETCH_SETTLE_TICK = 0.5
_FETCH_HTTP_TIMEOUT = 12
_FETCH_HTTP_MAX_BYTES = 1024 * 1024
_CLOSE_TIMEOUT_SECONDS = 3.0
_CONTENT_RETRY_TIMEOUT = 3.0
_CONTENT_RETRY_TICK = 0.2
_MAX_PAGE_CHARS = 200_000
_PDF_DOWNLOAD_TIMEOUT = 20
_PDF_CHUNK_BYTES = 64 * 1024
_PDF_MAX_REDIRECTS = 5
T = TypeVar("T")
_SEARCH_WORKER_LOCK = threading.Lock()
_SEARCH_WORKER: browser_worker.BrowserWorker | None = None


class SearchUnavailableError(RuntimeError):
    """A bounded, classified failure from the browser search surface."""

    def __init__(
        self,
        failure_kind: str,
        *,
        engine: str = "",
        detail: str = "",
        observed_host: str = "",
    ) -> None:
        self.failure_kind = str(failure_kind or "search_unavailable")
        self.engine = str(engine or "")
        self.detail = str(detail or "")
        self.observed_host = str(observed_host or "")
        message = self.failure_kind
        if self.engine:
            message = f"{message} ({self.engine})"
        if self.detail:
            message = f"{message}: {self.detail}"
        super().__init__(message)


def load_profiles() -> dict[str, Any]:
    try:
        return cast(dict[str, Any], json.loads(_PROFILES_PATH.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return {"default_engine": "bing", "engines": {}}


def _search_browser_call(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    return _search_browser_worker().call(fn, *args, **kwargs)


def _search_browser_worker() -> browser_worker.BrowserWorker:
    global _SEARCH_WORKER
    if _SEARCH_WORKER is None:
        with _SEARCH_WORKER_LOCK:
            if _SEARCH_WORKER is None:
                _SEARCH_WORKER = browser_worker.BrowserWorker(name="codey-research-browser")
    return _SEARCH_WORKER


class BrowserSearchProvider:
    name = "browser"

    def __init__(
        self,
        *,
        engine: str | None = None,
        profile_dir: Path | None = None,
        cdp_port: int = RESEARCH_CDP_PORT,
        browser_path: str | None = None,
        launch: bool = True,
        isolated: bool = True,
        bring_to_front: bool = False,
    ) -> None:
        profiles = load_profiles()
        engine_profiles = profiles.get("engines", {})
        if not isinstance(engine_profiles, dict):
            engine_profiles = {}
        self.engine = engine or profiles.get("default_engine", "bing")
        self._profiles = {
            str(name): value
            for name, value in engine_profiles.items()
            if isinstance(value, dict)
        }
        self._profile = self._profiles.get(self.engine)
        if not self._profile:
            raise ValueError(f"unknown search engine: {self.engine}")
        if engine is None:
            self._engine_order = tuple(
                dict.fromkeys(
                    [self.engine, *[name for name in self._profiles if name != self.engine]]
                )
            )
        else:
            self._engine_order = (self.engine,)
        self._reuse_url_contains = _search_host(self._profile)
        self.profile_dir = Path(profile_dir) if profile_dir else RESEARCH_PROFILE
        self.cdp_port = int(cdp_port)
        self.browser_path = browser_path
        self.launch = launch
        self.isolated = bool(isolated)
        self.bring_to_front = bool(bring_to_front)
        self._session: Any | None = None
        self._search_page = None
        self._fetch_page = None
        self._last_worker_health: dict[str, object] = {}
        self.last_search_errors: list[dict[str, str]] = []
        self.last_search_failure: dict[str, str] = {}

    def _ensure_session_on_browser_thread(self, *, reuse_url_contains: str = "") -> Any:
        if self._session is not None:
            return self._session
        target_reuse = "" if self.isolated else reuse_url_contains
        self._session = open_chat_page(
            "about:blank",
            target_reuse or "",
            port=self.cdp_port,
            profile=self.profile_dir,
            open_if_missing=self.launch,
            bring_to_front=False,
            isolated=self.isolated,
            fresh_tab=False,
            browser_path=self.browser_path,
        )
        return self._session

    def _prepare_page_on_browser_thread(self, page: Any) -> Any:
        page.set_default_navigation_timeout(_NAV_TIMEOUT_MS)
        if not getattr(page, "_codey_research_guarded", False):
            page.route("**/*", self._guard_request)
            with contextlib.suppress(Exception):
                page._codey_research_guarded = True
        return page

    def _ensure_search_page_on_browser_thread(self) -> Any:
        session = self._ensure_session_on_browser_thread(reuse_url_contains=self._reuse_url_contains)
        if self._page_closed_on_browser_thread(self._search_page):
            self._search_page = session.page
            if self.bring_to_front:
                self._bring_to_front_on_browser_thread(self._search_page)
        return self._prepare_page_on_browser_thread(self._search_page)

    def _replace_search_page_on_browser_thread(self) -> Any:
        session = self._ensure_session_on_browser_thread(reuse_url_contains=self._reuse_url_contains)
        context = self._page_context_on_browser_thread(self._search_page or session.page)
        page = context.new_page()
        self._search_page = page
        if self.bring_to_front:
            self._bring_to_front_on_browser_thread(page)
        return self._prepare_page_on_browser_thread(page)

    def _ensure_fetch_page_on_browser_thread(self, url: str) -> Any:
        session = self._ensure_session_on_browser_thread()
        if self._page_closed_on_browser_thread(self._fetch_page):
            context = self._page_context_on_browser_thread(self._search_page or session.page)
            self._fetch_page = context.new_page()
            if self.bring_to_front:
                self._bring_to_front_on_browser_thread(self._fetch_page)
        return self._prepare_page_on_browser_thread(self._fetch_page)

    def _page_context_on_browser_thread(self, page: Any) -> Any:
        try:
            return page.context
        except Exception:
            session = self._ensure_session_on_browser_thread()
            return session.browser.contexts[0] if session.browser.contexts else session.browser.new_context()

    def _page_closed_on_browser_thread(self, page: Any) -> bool:
        if page is None:
            return True
        try:
            return bool(page.is_closed())
        except Exception:
            return True

    def _bring_to_front_on_browser_thread(self, page: Any) -> None:
        with contextlib.suppress(Exception):
            page.bring_to_front()

    def _guard_request(self, route: Any) -> None:
        try:
            blocked = bool(check_fetch_url(route.request.url, use_cache=True))
        except Exception:
            blocked = True
        with contextlib.suppress(Exception):
            route.abort() if blocked else route.continue_()

    def search(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        cancellation.check()
        self.last_search_errors = []
        self.last_search_failure = {}
        try:
            results = _search_browser_call(self._search_on_browser_thread, query, limit)
        except (TimeoutError, cancellation.TaskCancelled, cancellation.DeadlineExceeded):
            self._record_worker_health()
            raise
        cancellation.check()
        return results

    def _search_on_browser_thread(self, query: str, limit: int) -> list[dict[str, Any]]:
        last_error: Exception | None = None
        deadline = time.monotonic() + _SEARCH_TOTAL_TIMEOUT_SECONDS
        for engine_index, engine in enumerate(self._engine_order):
            profile = self._profiles[engine]
            for attempt in range(_SEARCH_NAV_ATTEMPTS):
                if time.monotonic() >= deadline:
                    break
                page = (
                    self._ensure_search_page_on_browser_thread()
                    if engine_index == 0 and attempt == 0
                    else self._replace_search_page_on_browser_thread()
                )
                try:
                    return self._search_page_results_on_browser_thread(
                        page,
                        query,
                        limit,
                        profile=profile,
                        engine=engine,
                        deadline=deadline,
                    )
                except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
                    self._discard_search_page_on_browser_thread(page)
                    raise
                except Exception as exc:
                    last_error = exc
                    self._record_search_error(engine, exc)
                    self._discard_search_page_on_browser_thread(page)
        if time.monotonic() >= deadline and not isinstance(last_error, SearchUnavailableError):
            last_error = SearchUnavailableError(
                "search_timeout",
                engine=self._engine_order[-1] if self._engine_order else self.engine,
                detail="search budget exhausted",
            )
        if last_error is not None:
            if not self.last_search_failure:
                self._record_search_error(
                    getattr(last_error, "engine", "") or self.engine,
                    last_error,
                )
            raise SearchUnavailableError(
                getattr(last_error, "failure_kind", "") or _search_failure_kind(last_error),
                engine=getattr(last_error, "engine", "") or self.engine,
                detail=getattr(last_error, "detail", "") or _search_failure_detail(last_error),
            )
        raise SearchUnavailableError("search_unavailable", engine=self.engine)

    def _search_page_results_on_browser_thread(
        self,
        page: Any,
        query: str,
        limit: int,
        *,
        profile: dict[str, Any] | None = None,
        engine: str = "",
        deadline: float | None = None,
    ) -> list[dict[str, Any]]:
        active_engine = engine or self.engine
        active_profile = profile if isinstance(profile, dict) else self._profile
        if not isinstance(active_profile, dict):
            raise SearchUnavailableError("search_profile_missing", engine=active_engine, detail="search profile unavailable")
        if deadline is None:
            deadline = time.monotonic() + _SEARCH_TOTAL_TIMEOUT_SECONDS
        remaining_ms = int(max(250.0, (deadline - time.monotonic()) * 1000))
        with contextlib.suppress(Exception):
            page.set_default_navigation_timeout(min(_SEARCH_NAV_TIMEOUT_MS, remaining_ms))
        url = active_profile["search_url"].format(query=quote_plus(query))
        cancellation.check()
        page.goto(url, wait_until="domcontentloaded")
        cancellation.check()
        final_url = _page_url(page)
        expected_host = _search_engine_host(active_profile)
        if final_url and expected_host and not _same_host(final_url, expected_host):
            raise SearchUnavailableError(
                "search_wrong_host",
                engine=active_engine,
                detail="search page redirected to an unexpected host",
                observed_host=_url_host(final_url),
            )
        results: list[dict[str, Any]] = []
        for block in page.query_selector_all(active_profile["result_selector"])[: limit * 2]:
            cancellation.check()
            link = block.query_selector(active_profile["link_selector"])
            if link is None:
                continue
            href = _normalize_result_url(link.get_attribute("href") or "")
            if not _looks_like_public_result_url(href):
                continue
            title = _best_result_title(block, link, active_profile)
            if not title:
                title = _title_from_url(href)
            snippet_el = block.query_selector(active_profile["snippet_selector"])
            snippet = _element_text(snippet_el) if snippet_el else _snippet_from_block(block, title)
            results.append({"title": title, "url": href, "snippet": snippet})
            if len(results) >= limit:
                break
        if not results:
            cancellation.check()
            results = self._anchor_scan_results(page, limit)
        if results:
            cancellation.check()
            return results
        body_text = _search_page_body_text(page)
        if not _usable_search_page_text(body_text):
            raise SearchUnavailableError(
                _search_page_failure_kind(body_text),
                engine=active_engine,
                detail="search page was blank or unavailable",
            )
        cancellation.check()
        return []

    def _anchor_scan_results(self, page: Any, limit: int) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        for link in page.query_selector_all("a[href]"):
            cancellation.check()
            href = _normalize_result_url(link.get_attribute("href") or "")
            if href in seen or not _looks_like_public_result_url(href):
                continue
            title = _element_text(link) or str(link.get_attribute("aria-label") or "").strip()
            if not title:
                title = _title_from_url(href)
            if not title or _is_search_navigation_title(title):
                continue
            seen.add(href)
            results.append({"title": title, "url": href, "snippet": "", "truncated": False})
            if len(results) >= limit:
                break
        return results

    def fetch(self, url: str) -> dict[str, Any]:
        cancellation.check()
        if _is_pdf_url(url):
            page = _download_pdf_streaming(url)
        else:
            try:
                page = _search_browser_call(
                    self._fetch_on_browser_thread,
                    url,
                    timeout=_FETCH_TOTAL_TIMEOUT_SECONDS,
                    # If the caller gives up first, the abandoned job may
                    # leave the shared fetch page mid-navigation. Discard
                    # whatever page is current when the worker gets there;
                    # the discard is idempotent with the job's own paths.
                    on_abandoned=lambda: self._discard_fetch_page_on_browser_thread(
                        self._fetch_page
                    ),
                )
            except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
                self._record_worker_health()
                raise
            except TimeoutError as exc:
                self._record_worker_health()
                return _fetch_failure(url, f'could not load page within {_FETCH_TOTAL_TIMEOUT_SECONDS:.0f}s: {exc}')
            if page.get("content_kind") == "pdf_download":
                cancellation.check()
                page = _download_pdf_streaming(
                    str(page.get("url") or url),
                    mime_type=str(page.get("mime_type") or ""),
                )
        cancellation.check()
        return page

    def _fetch_on_browser_thread(self, url: str) -> dict[str, Any]:
        reason = check_fetch_url(url)
        if reason:
            return _fetch_failure(url, f'{reason}')
        if _is_pdf_url(url):
            return _pdf_download_sentinel(url)
        page = self._ensure_fetch_page_on_browser_thread(url)
        try:
            cancellation.check()
            _set_navigation_timeout(page, _FETCH_NAV_TIMEOUT_MS)
            try:
                response = page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=_FETCH_NAV_TIMEOUT_MS,
                )
            except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
                raise
            except Exception as exc:
                self._discard_fetch_page_on_browser_thread(page)
                return _fetch_failure(url, f'could not load page: {exc}')
            cancellation.check()
            final_url = page.url or url
            if final_url != url:
                reason = check_fetch_url(final_url)
                if reason:
                    return _fetch_failure(final_url, f'{reason} (after redirect)')
            if response is not None:
                ctype = (response.headers.get("content-type") or "").lower()
                if _is_pdf_response(ctype, final_url):
                    return _pdf_download_sentinel(final_url, mime_type=ctype)
                if ctype and not any(t in ctype for t in ("html", "text", "xml", "json")):
                    return _fetch_failure(final_url, f'unsupported content type: {ctype}', status="skipped")
            cancellation.check()
            try:
                html, text = _fetch_page_content_after_settle(page)
            except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
                raise
            except Exception as exc:
                self._discard_fetch_page_on_browser_thread(page)
                return _fetch_failure(final_url, f'could not read page content: {exc}')
            cancellation.check()
            if not _usable_fetch_page_text(text):
                title = extract_title(html)
                fallback = _download_text_fallback(final_url)
                fallback_text = str(fallback.get("text") or "")
                if fallback.get("status") == "ok" and fallback_text:
                    self._discard_fetch_page_on_browser_thread(page)
                    return fallback
                self._discard_fetch_page_on_browser_thread(page)
                return {"status": "ok",
                    "url": final_url,
                    "title": title,
                    "text": "ERROR: page had no usable visible content after navigation: "
                    + _fetch_page_failure_kind(text),
                    "truncated": False,
                }
            truncated = len(text) > _MAX_PAGE_CHARS
            if truncated:
                text = text[:_MAX_PAGE_CHARS]
            return {"status": "ok", "url": final_url, "title": extract_title(html), "text": text, "truncated": truncated}
        except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
            self._discard_fetch_page_on_browser_thread(page)
            raise

    def close(self) -> None:
        if self._session is None:
            return
        try:
            _search_browser_call(self._close_on_browser_thread, timeout=_CLOSE_TIMEOUT_SECONDS)
        except TimeoutError:
            self._record_worker_health()

    def worker_health(self) -> dict[str, object]:
        """Last passive research-browser worker health snapshot."""

        return dict(self._last_worker_health)

    def _record_worker_health(self) -> None:
        try:
            self._last_worker_health = _search_browser_worker().health_snapshot().to_payload()
        except Exception:
            self._last_worker_health = {"state": "unavailable", "stuck_detected": False}

    def _record_search_error(self, engine: str, exc: Exception) -> None:
        failure_kind = _search_failure_kind(exc)
        payload = {
            "engine": str(engine or self.engine)[:40],
            "failure_kind": failure_kind[:80],
            "error": _search_failure_detail(exc)[:160],
        }
        if isinstance(exc, SearchUnavailableError) and exc.engine:
            payload["engine"] = exc.engine[:40]
        observed_host = getattr(exc, "observed_host", "")
        if observed_host:
            payload["observed_host"] = str(observed_host)[:120]
        self.last_search_errors.append(payload)
        del self.last_search_errors[:-8]
        self.last_search_failure = dict(payload)

    def _close_on_browser_thread(self) -> None:
        try:
            seen_pages: set[int] = set()
            for page in (self._fetch_page, self._search_page):
                if page is None or id(page) in seen_pages:
                    continue
                seen_pages.add(id(page))
                self._release_page_guard_on_browser_thread(page)
            if self._session is not None:
                self._session.close()
        finally:
            self._fetch_page = None
            self._search_page = None
            self._session = None

    def _release_page_guard_on_browser_thread(self, page: Any) -> None:
        try:
            page.unroute("**/*", self._guard_request)
            page._codey_research_guarded = False
        except Exception:
            pass

    def _discard_page_on_browser_thread(self, page: Any) -> None:
        if page is None:
            return
        self._release_page_guard_on_browser_thread(page)
        with contextlib.suppress(Exception):
            page.close()

    def _discard_fetch_page_on_browser_thread(self, page: Any) -> None:
        self._discard_page_on_browser_thread(page)
        if page is self._fetch_page:
            self._fetch_page = None

    def _discard_search_page_on_browser_thread(self, page: Any) -> None:
        self._discard_page_on_browser_thread(page)
        if page is self._search_page:
            self._search_page = None


def _page_content_after_navigation(page: Any) -> str:
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


def _fetch_page_content_after_settle(page: Any) -> tuple[str, str]:
    html = _page_content_after_navigation(page)
    text = extract_text(html)
    if _usable_fetch_page_text(text):
        return html, text
    stop_at = time.monotonic() + _FETCH_SETTLE_TIMEOUT_SECONDS
    best_html = html
    best_text = text
    while time.monotonic() < stop_at:
        cancellation.check()
        with contextlib.suppress(Exception):
            page.wait_for_load_state("networkidle", timeout=500)
        cancellation.wait(_FETCH_SETTLE_TICK)
        html = _page_content_after_navigation(page)
        text = extract_text(html)
        if len(text) > len(best_text):
            best_html = html
            best_text = text
        if _usable_fetch_page_text(text):
            return html, text
    return best_html, best_text


def _content_retryable(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        "page is navigating" in text
        or "navigating and changing the content" in text
        or "execution context was destroyed" in text
    )


def _search_host(profile: dict[str, Any]) -> str:
    try:
        return (urlparse(str(profile.get("search_url") or "")).hostname or "").lower()
    except Exception:
        return ""


def _page_url(page: Any) -> str:
    try:
        value = page.url
    except Exception:
        return ""
    return value if isinstance(value, str) else ""


def _search_engine_host(profile: dict[str, Any]) -> str:
    try:
        return (urlparse(str(profile.get("search_url") or "")).hostname or "").lower().removeprefix("www.")
    except Exception:
        return ""


def _url_host(url: str) -> str:
    try:
        return (urlparse(str(url or "")).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""


def _same_host(url: str, expected_host: str) -> bool:
    try:
        actual = (urlparse(str(url or "")).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return False
    expected = str(expected_host or "").lower().removeprefix("www.")
    return bool(actual and expected and (actual == expected or actual.endswith("." + expected)))


def _search_page_body_text(page: Any) -> str:
    try:
        body = page.query_selector("body")
    except Exception:
        return ""
    return _element_text(body)


_SEARCH_PAGE_ERROR_MARKERS = (
    "access denied",
    "are you a robot",
    "captcha",
    "temporarily unavailable",
    "unusual traffic",
    "verify you are human",
    "something went wrong",
)
_SEARCH_PAGE_NO_RESULT_MARKERS = (
    "no results",
    "no results found",
    "didn't match any results",
    "did not match any results",
)


def _usable_search_page_text(text: str) -> bool:
    normalized = _clean_space(text).casefold()
    if not normalized:
        return False
    if any(marker in normalized for marker in _SEARCH_PAGE_ERROR_MARKERS):
        return False
    if any(marker in normalized for marker in _SEARCH_PAGE_NO_RESULT_MARKERS):
        return True
    return len(normalized) >= 24


def _search_page_failure_kind(text: str) -> str:
    normalized = _clean_space(text).casefold()
    if any(marker in normalized for marker in _SEARCH_PAGE_ERROR_MARKERS):
        return "search_page_unavailable"
    return "search_page_blank"


_FETCH_PAGE_ERROR_MARKERS = (
    "access denied",
    "are you a robot",
    "captcha",
    "checking your browser",
    "cookies must be enabled",
    "enable javascript",
    "enable cookies",
    "not automatically redirected",
    "temporarily unavailable",
    "unusual traffic",
    "verify you are human",
    "just a moment",
)
_FETCH_CHALLENGE_TEXT_MAX_CHARS = 1500


def _usable_fetch_page_text(text: str) -> bool:
    normalized = _clean_space(text).casefold()
    if not normalized:
        return False
    if _looks_like_short_fetch_challenge(normalized):
        return False
    return len(normalized) >= 24


def _fetch_page_failure_kind(text: str) -> str:
    normalized = _clean_space(text).casefold()
    if _looks_like_short_fetch_challenge(normalized):
        return "page_unavailable"
    return "page_blank"


def _looks_like_short_fetch_challenge(normalized_text: str) -> bool:
    if len(normalized_text) > _FETCH_CHALLENGE_TEXT_MAX_CHARS:
        return False
    return any(marker in normalized_text for marker in _FETCH_PAGE_ERROR_MARKERS)


def _set_navigation_timeout(page: Any, timeout_ms: int) -> None:
    with contextlib.suppress(Exception):
        page.set_default_navigation_timeout(timeout_ms)


def _search_failure_kind(exc: Exception) -> str:
    if isinstance(exc, SearchUnavailableError):
        return exc.failure_kind
    text = f"{type(exc).__name__} {exc}".casefold()
    if "timeout" in text or "timed out" in text:
        return "search_navigation_timeout"
    return "search_engine_error"


def _search_failure_detail(exc: Exception) -> str:
    if isinstance(exc, SearchUnavailableError) and exc.detail:
        return _clean_space(exc.detail)
    failure_kind = _search_failure_kind(exc)
    return {
        "search_navigation_timeout": "search navigation timed out",
        "search_page_unavailable": "search page reported an availability error",
        "search_page_blank": "search page had no usable visible content",
        "search_engine_error": type(exc).__name__,
    }.get(failure_kind, type(exc).__name__)


def _element_text(element: Any) -> str:
    return _clean_space(_element_raw_text(element))


def _element_raw_text(element: Any) -> str:
    if element is None:
        return ""
    for getter in ("inner_text", "text_content"):
        try:
            text = getattr(element, getter)()
        except Exception:
            text = ""
        text = str(text or "").strip()
        if text:
            return text
    for attr in ("aria-label", "title"):
        try:
            text = str(element.get_attribute(attr) or "").strip()
        except Exception:
            text = ""
        if text:
            return text
    return ""


def _best_result_title(block: Any, link: Any, profile: dict[str, Any]) -> str:
    selectors = [
        profile.get("title_selector"),
        profile.get("link_selector"),
        "h2",
        "h3",
    ]
    text = _element_text(link)
    if text:
        return text
    for selector in selectors:
        if not selector:
            continue
        try:
            text = _element_text(block.query_selector(selector))
        except Exception:
            text = ""
        if text:
            return text
    for line in _element_raw_text(block).splitlines():
        line = line.strip()
        if line and not line.lower().startswith(("http://", "https://")):
            return _clean_space(line)
    return ""


def _snippet_from_block(block: Any, title: str) -> str:
    lines = []
    for line in _element_raw_text(block).splitlines():
        line = line.strip()
        if not line or line == title or line.lower().startswith(("http://", "https://")):
            continue
        lines.append(line)
    return _clean_space(" ".join(lines[:2]))


def _looks_like_public_result_url(href: str) -> bool:
    parsed = urlparse((href or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    if _is_search_redirect(parsed):
        return False
    if _host_is_search_engine(host):
        path = parsed.path.lower()
        if path in ("", "/", "/search", "/html/"):
            return False
    return True


def _host_is_search_engine(host: str) -> bool:
    return any(host == domain or host.endswith("." + domain) for domain in ("bing.com", "duckduckgo.com"))


def _is_pdf_response(content_type: str, url: str) -> bool:
    ctype = str(content_type or "").lower()
    if "application/pdf" in ctype or "application/x-pdf" in ctype:
        return True
    return _is_pdf_url(url)


def _is_pdf_url(url: str) -> bool:
    path = urlparse(str(url or "")).path.lower()
    return path.endswith(".pdf")





def _download_text_fallback(url: str) -> dict[str, Any]:
    current_url = str(url or "").strip()
    redirects = 0
    while True:
        cancellation.check()
        reason = check_fetch_url(current_url)
        if reason:
            return _fetch_failure(current_url, f'{reason}')
        request = _text_request(current_url)
        try:
            response = _open_url_no_redirect(request, timeout=_FETCH_HTTP_TIMEOUT)
        except urllib.error.HTTPError as exc:
            if _is_redirect_status(exc.code):
                next_url = _redirect_target(current_url, exc.headers)
                _close_response(exc)
                redirect = _checked_redirect(current_url, next_url, redirects, content_kind="page")
                if redirect.get("error"):
                    return cast(dict[str, Any], redirect["error"])
                current_url = redirect["url"]
                redirects += 1
                continue
            return _fetch_failure(current_url, f'HTTP fallback could not load page: HTTP {exc.code}')
        except urllib.error.URLError as exc:
            return _fetch_failure(current_url, f'HTTP fallback could not load page: {exc}')
        except OSError as exc:
            return _fetch_failure(current_url, f'HTTP fallback could not load page: {exc}')
        with response:
            final_url = response.geturl() or current_url
            reason = check_fetch_url(final_url)
            if reason:
                return _fetch_failure(final_url, f'{reason} (after redirect)')
            status = int(getattr(response, "status", 0) or _response_code(response))
            if _is_redirect_status(status):
                next_url = _redirect_target(current_url, response.headers)
                redirect = _checked_redirect(current_url, next_url, redirects, content_kind="page")
                if redirect.get("error"):
                    return cast(dict[str, Any], redirect["error"])
                current_url = redirect["url"]
                redirects += 1
                continue
            headers = response.headers
            ctype = (headers.get("content-type") or "").lower()
            if _is_pdf_response(ctype, final_url):
                return _pdf_download_sentinel(final_url, mime_type=ctype)
            if ctype and not any(t in ctype for t in ("html", "text", "xml", "json")):
                return _fetch_failure(final_url, f'unsupported content type: {ctype}', status="skipped")
            data = response.read(_FETCH_HTTP_MAX_BYTES + 1)
            truncated = len(data) > _FETCH_HTTP_MAX_BYTES
            if truncated:
                data = data[:_FETCH_HTTP_MAX_BYTES]
            html = data.decode(response_charset(headers), errors="replace")
            text = extract_text(html)
            if not _usable_fetch_page_text(text):
                return {"status": "ok",
                    "url": final_url,
                    "title": extract_title(html),
                    "text": "ERROR: HTTP fallback had no usable visible content: "
                    + _fetch_page_failure_kind(text),
                    "truncated": truncated,
                }
            text_truncated = len(text) > _MAX_PAGE_CHARS
            if text_truncated:
                text = text[:_MAX_PAGE_CHARS]
            return {"status": "ok",
                "url": final_url,
                "title": extract_title(html) or _title_from_url(final_url),
                "text": text,
                "truncated": truncated or text_truncated,
            }


def _content_length(headers: dict[str, Any]) -> int | None:
    try:
        value = headers.get("content-length")
    except AttributeError:
        value = None
    try:
        return int(str(value or "").strip())
    except ValueError:
        return None


def _download_pdf_streaming(url: str, *, mime_type: str = "") -> dict[str, Any]:
    current_url = str(url or "").strip()
    redirects = 0
    while True:
        cancellation.check()
        reason = check_fetch_url(current_url)
        if reason:
            return _fetch_failure(current_url, f'{reason}')
        request = _pdf_request(current_url)
        try:
            response = _open_url_no_redirect(request, timeout=_PDF_DOWNLOAD_TIMEOUT)
        except urllib.error.HTTPError as exc:
            if _is_redirect_status(exc.code):
                next_url = _redirect_target(current_url, exc.headers)
                _close_response(exc)
                redirect = _checked_redirect(current_url, next_url, redirects)
                if redirect.get("error"):
                    return cast(dict[str, Any], redirect["error"])
                current_url = redirect["url"]
                redirects += 1
                continue
            return _pdf_skipped(
                current_url, mime_type or "application/pdf", f"PDF could not be downloaded: HTTP {exc.code}"
            )
        except urllib.error.URLError as exc:
            return _pdf_skipped(current_url, mime_type or "application/pdf", f"PDF could not be downloaded: {exc}")
        except OSError as exc:
            return _pdf_skipped(current_url, mime_type or "application/pdf", f"PDF could not be downloaded: {exc}")
        with response:
            final_url = response.geturl() or current_url
            reason = check_fetch_url(final_url)
            if reason:
                return _fetch_failure(final_url, f'{reason} (after redirect)')
            status = int(getattr(response, "status", 0) or _response_code(response))
            if _is_redirect_status(status):
                next_url = _redirect_target(current_url, response.headers)
                redirect = _checked_redirect(current_url, next_url, redirects)
                if redirect.get("error"):
                    return cast(dict[str, Any], redirect["error"])
                current_url = redirect["url"]
                redirects += 1
                continue
            headers = response.headers
            ctype = (headers.get("content-type") or mime_type or "application/pdf").lower()
            length = _content_length(headers)
            if length is not None and length > PDF_MAX_BYTES:
                return _pdf_skipped(
                    final_url, ctype, f"PDF is too large to read safely ({length} bytes > {PDF_MAX_BYTES})"
                )
            body = bytearray()
            cancellation.check()
            while True:
                chunk = response.read(_PDF_CHUNK_BYTES)
                cancellation.check()
                if not chunk:
                    break
                body.extend(chunk)
                if len(body) > PDF_MAX_BYTES:
                    return _pdf_skipped(final_url, ctype, f"PDF is too large to read safely (> {PDF_MAX_BYTES} bytes)")
            if not _is_pdf_response(ctype, final_url):
                return _fetch_failure(final_url, f'unsupported content type: {ctype}', status="skipped")
            return {"status": "ok",
                "url": final_url,
                "title": _title_from_url(final_url),
                "text": "",
                "content_kind": "pdf",
                "mime_type": ctype or "application/pdf",
                "bytes": bytes(body),
                "truncated": False,
            }


def _pdf_request(url: str) -> urllib.request.Request:
    return urllib.request.Request(
        url,
        headers={
            "Accept": "application/pdf,*/*;q=0.8",
            "User-Agent": "Research PDF Reader",
        },
    )


def _text_request(url: str) -> urllib.request.Request:
    return urllib.request.Request(
        url,
        headers={
            "Accept": "text/html,application/xhtml+xml,application/xml,text/xml,text/plain,*/*;q=0.8",
            "User-Agent": "Mozilla/5.0 Research Reader",
        },
    )


def _open_url_no_redirect(request: urllib.request.Request, *, timeout: int) -> Any:
    opener = build_no_redirect_opener()
    return opener.open(request, timeout=timeout)


def _pdf_download_sentinel(url: str, *, mime_type: str = "") -> dict[str, Any]:
    return {"status": "ok",
        "url": url,
        "title": _title_from_url(url),
        "text": "",
        "content_kind": "pdf_download",
        "mime_type": mime_type or "application/pdf",
        "truncated": False,
    }


def _checked_redirect(
    current_url: str,
    next_url: str,
    redirects: int,
    *,
    content_kind: str = "pdf",
) -> dict[str, Any]:
    if not next_url:
        return {"error": _redirect_error(current_url, content_kind, "redirect did not include a Location header")}
    if redirects >= _PDF_MAX_REDIRECTS:
        return {"error": _redirect_error(current_url, content_kind, "redirect limit exceeded")}
    reason = check_fetch_url(next_url)
    if reason:
        return {
            "error": _fetch_failure(next_url, f'{reason} (after redirect)')
        }
    return {"url": next_url}


def _response_code(response: Any) -> int:
    try:
        return int(response.getcode() or 0)
    except Exception:
        return 0


def _fetch_failure(url: str, detail: str, *, status: str = "error", title: str = "",
                   content_kind: str = "", mime_type: str = "") -> dict[str, Any]:
    prefix = "SKIPPED" if status == "skipped" else "ERROR"
    payload = {"status": status, "detail": detail, "url": url, "title": title,
               "text": f"{prefix}: {detail}", "truncated": False}
    if content_kind:
        payload["content_kind"] = content_kind
    if mime_type:
        payload["mime_type"] = mime_type
    return payload


def _pdf_skipped(url: str, mime_type: str, message: str) -> dict[str, Any]:
    return _fetch_failure(url, f'{message}', status="skipped", title=_title_from_url(url), content_kind="pdf", mime_type=mime_type or "application/pdf")


def _redirect_error(url: str, content_kind: str, message: str) -> dict[str, Any]:
    if str(content_kind or "").casefold() == "pdf":
        return _pdf_skipped(url, "application/pdf", "PDF " + message)
    return _fetch_failure(url, f'page {message}', title=_title_from_url(url))


def _normalize_result_url(href: str) -> str:
    raw = str(href or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        return raw
    target = _search_redirect_target(parsed)
    return target or raw


def _is_search_redirect(parsed: Any) -> bool:
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    params = parse_qs(parsed.query or "")
    if (host == "bing.com" or host.endswith(".bing.com")) and path.startswith("/ck/"):
        return True
    return bool(
        (host == "duckduckgo.com" or host.endswith(".duckduckgo.com"))
        and any(key in params for key in ("uddg", "u")))


def _search_redirect_target(parsed: Any) -> str:
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    params = parse_qs(parsed.query or "")
    if (host == "bing.com" or host.endswith(".bing.com")) and path.startswith("/ck/"):
        for key in ("u", "r", "url"):
            for value in params.get(key, []):
                target = _decode_redirect_value(value)
                if target:
                    return target
    if host == "duckduckgo.com" or host.endswith(".duckduckgo.com"):
        for key in ("uddg", "u"):
            for value in params.get(key, []):
                target = _decode_redirect_value(value)
                if target:
                    return target
    return ""


def _decode_redirect_value(value: str) -> str:
    raw = unquote(str(value or "").strip())
    if raw.startswith(("http://", "https://")):
        return raw
    candidates = [raw]
    if raw.startswith("a1") and len(raw) > 2:
        candidates.insert(0, raw[2:])
    for item in candidates:
        try:
            padded = item + ("=" * (-len(item) % 4))
            decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8", errors="replace")
        except (binascii.Error, UnicodeError, ValueError):
            continue
        decoded = decoded.strip()
        if decoded.startswith(("http://", "https://")):
            return decoded
    return ""


def _title_from_url(href: str) -> str:
    parsed = urlparse(href)
    return parsed.netloc or href


def _clean_space(text: str) -> str:
    return " ".join(str(text or "").split())


def _is_search_navigation_title(title: str) -> bool:
    lower = title.lower()
    return lower in {"next", "previous", "more", "search", "bing", "duckduckgo"}
