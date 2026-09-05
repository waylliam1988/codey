"""Zero-key web search and page fetch for Research."""

from __future__ import annotations

import base64
import binascii
import json
from pathlib import Path
import threading
import time
from typing import Any, Callable, TypeVar
import urllib.error
from urllib.parse import parse_qs, quote_plus, unquote, urlparse
import urllib.request

from codey.runtime import cancellation
from codey.automation import browser_worker
from codey.automation.browser import DEFAULT_PORT, open_chat_page
from codey.storage.local_store import DEFAULT_STATE_HOME
from codey.research.extract import extract_text, extract_title
from codey.research.http_redirects import (
    build_no_redirect_opener,
    close_response as _close_response,
    is_redirect_status as _is_redirect_status,
    redirect_target as _redirect_target,
)
from codey.research.pdf_extract import PDF_MAX_BYTES
from codey.policies.network import check_fetch_url

_PROFILES_PATH = Path(__file__).with_name("search_profiles.json")
RESEARCH_PROFILE = DEFAULT_STATE_HOME / "research-edge-profile"
RESEARCH_CDP_PORT = DEFAULT_PORT + 40
_NAV_TIMEOUT_MS = 20_000
_SEARCH_NAV_TIMEOUT_MS = 8_000
_SEARCH_NAV_ATTEMPTS = 2
_SEARCH_TOTAL_TIMEOUT_SECONDS = 24.0
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


def load_profiles() -> dict:
    try:
        return json.loads(_PROFILES_PATH.read_text(encoding="utf-8"))
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
        self._session = None
        self._search_page = None
        self._fetch_page = None
        self._last_worker_health: dict[str, object] = {}
        self.last_search_errors: list[dict[str, str]] = []
        self.last_search_failure: dict[str, str] = {}

    def _ensure_session_on_browser_thread(self, *, reuse_url_contains: str = ""):
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

    def _prepare_page_on_browser_thread(self, page):
        page.set_default_navigation_timeout(_NAV_TIMEOUT_MS)
        if not getattr(page, "_codey_research_guarded", False):
            page.route("**/*", self._guard_request)
            try:
                setattr(page, "_codey_research_guarded", True)
            except Exception:
                pass
        return page

    def _ensure_search_page_on_browser_thread(self):
        session = self._ensure_session_on_browser_thread(reuse_url_contains=self._reuse_url_contains)
        if self._page_closed_on_browser_thread(self._search_page):
            self._search_page = session.page
            if self.bring_to_front:
                self._bring_to_front_on_browser_thread(self._search_page)
        return self._prepare_page_on_browser_thread(self._search_page)

    def _replace_search_page_on_browser_thread(self):
        session = self._ensure_session_on_browser_thread(reuse_url_contains=self._reuse_url_contains)
        context = self._page_context_on_browser_thread(self._search_page or session.page)
        page = context.new_page()
        self._search_page = page
        if self.bring_to_front:
            self._bring_to_front_on_browser_thread(page)
        return self._prepare_page_on_browser_thread(page)

    def _ensure_fetch_page_on_browser_thread(self, url: str):
        session = self._ensure_session_on_browser_thread()
        if self._page_closed_on_browser_thread(self._fetch_page):
            context = self._page_context_on_browser_thread(self._search_page or session.page)
            self._fetch_page = context.new_page()
            if self.bring_to_front:
                self._bring_to_front_on_browser_thread(self._fetch_page)
        return self._prepare_page_on_browser_thread(self._fetch_page)

    def _page_context_on_browser_thread(self, page):
        try:
            return page.context
        except Exception:
            session = self._ensure_session_on_browser_thread()
            return session.browser.contexts[0] if session.browser.contexts else session.browser.new_context()

    def _page_closed_on_browser_thread(self, page) -> bool:
        if page is None:
            return True
        try:
            return bool(page.is_closed())
        except Exception:
            return True

    def _bring_to_front_on_browser_thread(self, page) -> None:
        try:
            page.bring_to_front()
        except Exception:
            pass

    def _guard_request(self, route) -> None:
        try:
            blocked = bool(check_fetch_url(route.request.url, use_cache=True))
        except Exception:
            blocked = True
        try:
            route.abort() if blocked else route.continue_()
        except Exception:
            pass

    def search(self, query: str, limit: int = 8) -> list[dict]:
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

    def _search_on_browser_thread(self, query: str, limit: int) -> list[dict]:
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
        page,
        query: str,
        limit: int,
        *,
        profile: dict | None = None,
        engine: str = "",
        deadline: float | None = None,
    ) -> list[dict]:
        active_profile = profile or self._profile
        active_engine = engine or self.engine
        if deadline is None:
            deadline = time.monotonic() + _SEARCH_TOTAL_TIMEOUT_SECONDS
        remaining_ms = int(max(250.0, (deadline - time.monotonic()) * 1000))
        try:
            page.set_default_navigation_timeout(min(_SEARCH_NAV_TIMEOUT_MS, remaining_ms))
        except Exception:
            pass
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
        results: list[dict] = []
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
            results = self._fallback_results(page, limit)
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

    def _fallback_results(self, page, limit: int) -> list[dict]:
        results: list[dict] = []
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
            results.append({"title": title, "url": href, "snippet": ""})
            if len(results) >= limit:
                break
        return results

    def fetch(self, url: str) -> dict:
        cancellation.check()
        if _is_pdf_url(url):
            page = _download_pdf_streaming(url)
        else:
            try:
                page = _search_browser_call(self._fetch_on_browser_thread, url)
            except (TimeoutError, cancellation.TaskCancelled, cancellation.DeadlineExceeded):
                self._record_worker_health()
                raise
            if page.get("content_kind") == "pdf_download":
                cancellation.check()
                page = _download_pdf_streaming(
                    str(page.get("url") or url),
                    mime_type=str(page.get("mime_type") or ""),
                )
        cancellation.check()
        return page

    def _fetch_on_browser_thread(self, url: str) -> dict:
        reason = check_fetch_url(url)
        if reason:
            return {"url": url, "title": "", "text": f"ERROR: {reason}", "truncated": False}
        if _is_pdf_url(url):
            return _pdf_download_sentinel(url)
        page = self._ensure_fetch_page_on_browser_thread(url)
        try:
            cancellation.check()
            try:
                response = page.goto(url, wait_until="domcontentloaded")
            except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
                raise
            except Exception as exc:
                self._discard_fetch_page_on_browser_thread(page)
                return {"url": url, "title": "", "text": f"ERROR: could not load page: {exc}", "truncated": False}
            cancellation.check()
            final_url = page.url or url
            if final_url != url:
                reason = check_fetch_url(final_url)
                if reason:
                    return {
                        "url": final_url,
                        "title": "",
                        "text": f"ERROR: {reason} (after redirect)",
                        "truncated": False,
                    }
            if response is not None:
                ctype = (response.headers.get("content-type") or "").lower()
                if _is_pdf_response(ctype, final_url):
                    return _pdf_download_sentinel(final_url, mime_type=ctype)
                if ctype and not any(t in ctype for t in ("html", "text", "xml", "json")):
                    return {
                        "url": final_url,
                        "title": "",
                        "text": f"ERROR: unsupported content type: {ctype}",
                        "truncated": False,
                    }
            cancellation.check()
            html = _page_content_after_navigation(page)
            cancellation.check()
            text = extract_text(html)
            truncated = len(text) > _MAX_PAGE_CHARS
            if truncated:
                text = text[:_MAX_PAGE_CHARS]
            return {"url": final_url, "title": extract_title(html), "text": text, "truncated": truncated}
        except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
            self._discard_fetch_page_on_browser_thread(page)
            raise

    def close(self) -> None:
        if self._session is None:
            return
        _search_browser_call(self._close_on_browser_thread)

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

    def _release_page_guard_on_browser_thread(self, page) -> None:
        try:
            page.unroute("**/*", self._guard_request)
            setattr(page, "_codey_research_guarded", False)
        except Exception:
            pass

    def _discard_page_on_browser_thread(self, page) -> None:
        if page is None:
            return
        self._release_page_guard_on_browser_thread(page)
        try:
            page.close()
        except Exception:
            pass

    def _discard_fetch_page_on_browser_thread(self, page) -> None:
        self._discard_page_on_browser_thread(page)
        if page is self._fetch_page:
            self._fetch_page = None

    def _discard_search_page_on_browser_thread(self, page) -> None:
        self._discard_page_on_browser_thread(page)
        if page is self._search_page:
            self._search_page = None


def _page_content_after_navigation(page) -> str:
    stop_at = time.monotonic() + _CONTENT_RETRY_TIMEOUT
    while True:
        cancellation.check()
        try:
            return str(page.content() or "")
        except Exception as exc:
            if not _content_retryable(exc) or time.monotonic() >= stop_at:
                raise
            try:
                page.wait_for_load_state("domcontentloaded", timeout=500)
            except Exception:
                pass
            cancellation.wait(_CONTENT_RETRY_TICK)


def _content_retryable(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        "page is navigating" in text
        or "navigating and changing the content" in text
        or "execution context was destroyed" in text
    )


def _search_host(profile: dict) -> str:
    try:
        return urlparse(str(profile.get("search_url") or "")).netloc
    except Exception:
        return ""


def _page_url(page) -> str:
    try:
        value = page.url
    except Exception:
        return ""
    return value if isinstance(value, str) else ""


def _search_engine_host(profile: dict) -> str:
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


def _search_page_body_text(page) -> str:
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


def _element_text(element) -> str:
    return _clean_space(_element_raw_text(element))


def _element_raw_text(element) -> str:
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


def _best_result_title(block, link, profile: dict) -> str:
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


def _snippet_from_block(block, title: str) -> str:
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
    if any(part in host for part in ("bing.com", "duckduckgo.com")):
        path = parsed.path.lower()
        if path in ("", "/", "/search", "/html/"):
            return False
    return True


def _is_pdf_response(content_type: str, url: str) -> bool:
    ctype = str(content_type or "").lower()
    if "application/pdf" in ctype or "application/x-pdf" in ctype:
        return True
    return _is_pdf_url(url)


def _is_pdf_url(url: str) -> bool:
    path = urlparse(str(url or "")).path.lower()
    return path.endswith(".pdf")


def _content_length(headers: dict) -> int | None:
    try:
        value = headers.get("content-length")
    except AttributeError:
        value = None
    try:
        return int(str(value or "").strip())
    except ValueError:
        return None


def _download_pdf_streaming(url: str, *, mime_type: str = "") -> dict:
    current_url = str(url or "").strip()
    redirects = 0
    while True:
        cancellation.check()
        reason = check_fetch_url(current_url)
        if reason:
            return {"url": current_url, "title": "", "text": f"ERROR: {reason}", "truncated": False}
        request = _pdf_request(current_url)
        try:
            response = _open_url_no_redirect(request, timeout=_PDF_DOWNLOAD_TIMEOUT)
        except urllib.error.HTTPError as exc:
            if _is_redirect_status(exc.code):
                next_url = _redirect_target(current_url, exc.headers)
                _close_response(exc)
                redirect = _checked_redirect(current_url, next_url, redirects)
                if redirect.get("error"):
                    return redirect["error"]
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
                return {"url": final_url, "title": "", "text": f"ERROR: {reason} (after redirect)", "truncated": False}
            status = int(getattr(response, "status", 0) or _response_code(response))
            if _is_redirect_status(status):
                next_url = _redirect_target(current_url, response.headers)
                redirect = _checked_redirect(current_url, next_url, redirects)
                if redirect.get("error"):
                    return redirect["error"]
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
                return {
                    "url": final_url,
                    "title": "",
                    "text": f"ERROR: unsupported content type: {ctype}",
                    "truncated": False,
                }
            return {
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


def _open_url_no_redirect(request: urllib.request.Request, *, timeout: int):
    opener = build_no_redirect_opener()
    return opener.open(request, timeout=timeout)


def _pdf_download_sentinel(url: str, *, mime_type: str = "") -> dict:
    return {
        "url": url,
        "title": _title_from_url(url),
        "text": "",
        "content_kind": "pdf_download",
        "mime_type": mime_type or "application/pdf",
        "truncated": False,
    }


def _checked_redirect(current_url: str, next_url: str, redirects: int) -> dict:
    if not next_url:
        return {"error": _pdf_skipped(current_url, "application/pdf", "PDF redirect did not include a Location header")}
    if redirects >= _PDF_MAX_REDIRECTS:
        return {"error": _pdf_skipped(current_url, "application/pdf", "PDF redirect limit exceeded")}
    reason = check_fetch_url(next_url)
    if reason:
        return {
            "error": {"url": next_url, "title": "", "text": f"ERROR: {reason} (after redirect)", "truncated": False}
        }
    return {"url": next_url}


def _response_code(response) -> int:
    try:
        return int(response.getcode() or 0)
    except Exception:
        return 0


def _pdf_skipped(url: str, mime_type: str, message: str) -> dict:
    return {
        "url": url,
        "title": _title_from_url(url),
        "text": f"SKIPPED: {message}",
        "content_kind": "pdf",
        "mime_type": mime_type or "application/pdf",
        "truncated": False,
    }


def _normalize_result_url(href: str) -> str:
    raw = str(href or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        return raw
    target = _search_redirect_target(parsed)
    return target or raw


def _is_search_redirect(parsed) -> bool:
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    params = parse_qs(parsed.query or "")
    if host.endswith("bing.com") and path.startswith("/ck/"):
        return True
    if host.endswith("duckduckgo.com") and any(key in params for key in ("uddg", "u")):
        return True
    return False


def _search_redirect_target(parsed) -> str:
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    params = parse_qs(parsed.query or "")
    if host.endswith("bing.com") and path.startswith("/ck/"):
        for key in ("u", "r", "url"):
            for value in params.get(key, []):
                target = _decode_redirect_value(value)
                if target:
                    return target
    if host.endswith("duckduckgo.com"):
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
