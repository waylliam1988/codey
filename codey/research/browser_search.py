"""Zero-key web search and page fetch for Research."""

from __future__ import annotations

import base64
import binascii
import contextlib
import json
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar, cast
from urllib.parse import ParseResult, parse_qs, quote_plus, unquote, urlparse

from playwright.sync_api import BrowserContext, ElementHandle, Page, Request, Response, Route
from playwright.sync_api import Error as BrowserError

from codey.automation import browser_worker
from codey.automation.browser import DEFAULT_PORT, Session, open_chat_page, open_headless_page
from codey.policies.network import check_fetch_url
from codey.research import source_download
from codey.research.extract import (
    _MAX_PAGE_CHARS,
    _clean_space,
    _fetch_page_failure_kind,
    _usable_fetch_page_text,
    extract_text,
    extract_title,
)
from codey.research.source_acquisition import SourceFailureCode, http_failure_code, with_attempt
from codey.runtime.core import cancellation
from codey.storage.local_store import DEFAULT_STATE_HOME
from codey.types.json_value import JsonObject, JsonValue, decode_json_object

_PROFILES_PATH = Path(__file__).with_name("search_profiles.json")
RESEARCH_PROFILE = DEFAULT_STATE_HOME / "research-edge-profile"
RESEARCH_CDP_PORT = DEFAULT_PORT + 40
_NAV_TIMEOUT_MS = 20_000
_SEARCH_NAV_TIMEOUT_MS = 8_000
_SEARCH_NAV_ATTEMPTS = 2
_SEARCH_TOTAL_TIMEOUT_SECONDS = 24.0
_FETCH_NAV_TIMEOUT_MS = 10_000
_FETCH_TOTAL_TIMEOUT_SECONDS = 16.0
_FETCH_SETTLE_TIMEOUT_SECONDS = 8.0
_FETCH_SETTLE_TICK = 0.5
_CLOSE_TIMEOUT_SECONDS = 3.0
_CONTENT_RETRY_TIMEOUT = 3.0
_CONTENT_RETRY_TICK = 0.2
_PAGE_GUARD_ATTRIBUTE = "_codey_research_guarded"
T = TypeVar("T")
_SEARCH_WORKER_LOCK = threading.Lock()
_SEARCH_WORKER: browser_worker.BrowserWorker | None = None


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
        self.failure_kind = (failure_kind or "search_unavailable")
        self.engine = (engine or "")
        self.detail = (detail or "")
        self.observed_host = (observed_host or "")
        message = self.failure_kind
        if self.engine:
            message = f"{message} ({self.engine})"
        if self.detail:
            message = f"{message}: {self.detail}"
        super().__init__(message)


def load_profiles() -> JsonObject:
    try:
        return decode_json_object(json.loads(_PROFILES_PATH.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return {"default_engine": "bing", "engines": {}}


def _search_browser_call(
    fn: Callable[..., T], *args: object,
    timeout: float | None = None,
    on_abandoned: Callable[[], None] | None = None,
    **kwargs: object,
) -> T:
    return _search_browser_worker().call(
        fn, *args, timeout=timeout, on_abandoned=on_abandoned, **kwargs,
    )


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
        require_existing_session: bool = False,
        bring_to_front: bool = False,
        headless: bool = False,
    ) -> None:
        profiles = load_profiles()
        engine_profiles_value = profiles.get("engines", {})
        engine_profiles: dict[str, JsonValue] = (
            engine_profiles_value if isinstance(engine_profiles_value, dict) else {}
        )
        self.engine = engine or str(profiles.get("default_engine", "bing"))
        self._profiles: dict[str, JsonObject] = {
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
        self.cdp_port = cdp_port
        self.browser_path = browser_path
        self.launch = launch
        self.isolated = isolated
        self.require_existing_session = bool(require_existing_session)
        self.bring_to_front = bring_to_front
        self.headless = headless
        self._session: Session | None = None
        self._search_page: Page | None = None
        self._fetch_page: Page | None = None
        self._last_worker_health: dict[str, object] = {}
        self.last_search_errors: list[dict[str, str]] = []
        self.last_search_failure: dict[str, str] = {}

    def _ensure_session_on_browser_thread(self, *, reuse_url_contains: str = "") -> Session:
        if self._session is not None:
            connected = getattr(self._session.browser, "is_connected", None)
            if callable(connected) and not connected():
                self._close_on_browser_thread()
        if self._session is not None:
            return self._session
        if self.headless:
            self._session = open_headless_page(browser_path=self.browser_path)
            # Context routing also covers the first request of script-opened
            # pages, which a route attached only to the main page cannot see.
            self._session.page.context.route("**/*", self._guard_request)
            return self._session
        target_reuse = "" if self.isolated else reuse_url_contains
        self._session = open_chat_page(
            "about:blank",
            target_reuse or "",
            port=self.cdp_port,
            profile=self.profile_dir,
            open_if_missing=self.launch and not self.require_existing_session,
            bring_to_front=False,
            isolated=self.isolated,
            fresh_tab=False,
            browser_path=self.browser_path,
            allow_new_page=True,
        )
        return self._session

    def _prepare_page_on_browser_thread(self, page: Page) -> Page:
        page.set_default_navigation_timeout(_NAV_TIMEOUT_MS)
        if not self.headless and not getattr(page, _PAGE_GUARD_ATTRIBUTE, False):
            page.route("**/*", self._guard_request)
            with contextlib.suppress(Exception):
                setattr(page, _PAGE_GUARD_ATTRIBUTE, True)
        return page

    def _ensure_search_page_on_browser_thread(self) -> Page:
        session = self._ensure_session_on_browser_thread(reuse_url_contains=self._reuse_url_contains)
        if self._page_closed_on_browser_thread(self._search_page):
            self._search_page = session.page
            if self._page_closed_on_browser_thread(self._search_page):
                self._search_page = self._page_context_on_browser_thread(session.page).new_page()
            if self.bring_to_front:
                self._bring_to_front_on_browser_thread(self._search_page)
        return self._prepare_page_on_browser_thread(cast(Page, self._search_page))

    def _replace_search_page_on_browser_thread(self) -> Page:
        session = self._ensure_session_on_browser_thread(reuse_url_contains=self._reuse_url_contains)
        context = self._page_context_on_browser_thread(self._search_page or session.page)
        page = context.new_page()
        self._search_page = page
        if self.bring_to_front:
            self._bring_to_front_on_browser_thread(page)
        return self._prepare_page_on_browser_thread(page)

    def _ensure_fetch_page_on_browser_thread(self, url: str) -> Page:
        session = self._ensure_session_on_browser_thread()
        if self._page_closed_on_browser_thread(self._fetch_page):
            context = self._page_context_on_browser_thread(self._search_page or session.page)
            self._fetch_page = context.new_page()
            if self.bring_to_front:
                self._bring_to_front_on_browser_thread(self._fetch_page)
        return self._prepare_page_on_browser_thread(cast(Page, self._fetch_page))

    def _prepare_fetch_page_on_browser_thread(self, url: str) -> Page:
        cancellation.check()
        try:
            return self._ensure_fetch_page_on_browser_thread(url)
        except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
            raise
        except Exception as exc:
            if "has been closed" not in str(exc).lower():
                raise
            # Reconnect before navigation, at most once; a connected CDP
            # transport can still retain a context that has already closed.
            with contextlib.suppress(Exception):
                self._close_on_browser_thread()
            cancellation.check()
            return self._ensure_fetch_page_on_browser_thread(url)

    def _page_context_on_browser_thread(self, page: Page) -> BrowserContext:
        try:
            return page.context
        except Exception:
            session = self._ensure_session_on_browser_thread()
            return session.browser.contexts[0] if session.browser.contexts else session.browser.new_context()

    def _page_closed_on_browser_thread(self, page: Page | None) -> bool:
        if page is None:
            return True
        try:
            return bool(page.is_closed())
        except Exception:
            return True

    def _bring_to_front_on_browser_thread(self, page: Page) -> None:
        with contextlib.suppress(Exception):
            page.bring_to_front()

    def _guard_request(self, route: Route) -> None:
        try:
            blocked = bool(check_fetch_url(route.request.url, use_cache=True))
        except Exception:
            blocked = True
        with contextlib.suppress(Exception):
            route.abort() if blocked else route.continue_()

    def search(self, query: str, limit: int = 8) -> list[JsonObject]:
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

    def _search_on_browser_thread(self, query: str, limit: int) -> list[JsonObject]:
        last_error: Exception | None = None
        deadline = time.monotonic() + _SEARCH_TOTAL_TIMEOUT_SECONDS
        for engine_index, engine in enumerate(self._engine_order):
            profile = self._profiles[engine]
            # Reserve time for the remaining engines instead of letting the
            # first engine consume the entire search deadline with retries.
            engine_deadline = time.monotonic() + max(0.0, deadline - time.monotonic()) / (
                len(self._engine_order) - engine_index
            )
            for attempt in range(_SEARCH_NAV_ATTEMPTS):
                if time.monotonic() >= engine_deadline:
                    break
                page = None
                try:
                    page = (
                        self._ensure_search_page_on_browser_thread()
                        if engine_index == 0 and attempt == 0
                        else self._replace_search_page_on_browser_thread()
                    )
                    return self._search_page_results_on_browser_thread(
                        page,
                        query,
                        limit,
                        profile=profile,
                        engine=engine,
                        deadline=engine_deadline,
                    )
                except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
                    self._discard_search_page_on_browser_thread(page)
                    raise
                except Exception as exc:
                    last_error = exc
                    self._record_search_error(engine, exc)
                    self._discard_search_page_on_browser_thread(page)
                    if "has been closed" in str(exc).lower():
                        # A dead context cannot create a replacement page.
                        # Drop the cached connection before the bounded retry.
                        with contextlib.suppress(Exception):
                            self._close_on_browser_thread()
        if time.monotonic() >= deadline and not isinstance(last_error, SearchUnavailableError):
            last_error = SearchUnavailableError(
                "search_timeout",
                engine=self._engine_order[-1] if self._engine_order else self.engine,
                detail="search time budget exhausted",
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
        page: Page,
        query: str,
        limit: int,
        *,
        profile: JsonObject | None = None,
        engine: str = "",
        deadline: float | None = None,
    ) -> list[JsonObject]:
        active_engine = engine or self.engine
        active_profile = profile if isinstance(profile, dict) else self._profile
        if not isinstance(active_profile, dict):
            raise SearchUnavailableError("search_profile_missing", engine=active_engine, detail="search profile unavailable")
        if deadline is None:
            deadline = time.monotonic() + _SEARCH_TOTAL_TIMEOUT_SECONDS
        remaining_ms = int(max(250.0, (deadline - time.monotonic()) * 1000))
        with contextlib.suppress(Exception):
            page.set_default_navigation_timeout(min(_SEARCH_NAV_TIMEOUT_MS, remaining_ms))
        url = str(active_profile["search_url"]).format(query=quote_plus(query))
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
        # Search pages may paint navigation before their result blocks. A
        # populated page body alone must not win that race as "no results".
        wait = getattr(page, "wait_for_selector", None)
        remaining_ms = int(max(0.0, deadline - time.monotonic()) * 1000)
        if callable(wait) and remaining_ms:
            try:
                wait(str(active_profile["result_selector"]), state="attached", timeout=min(2500, remaining_ms))
            except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
                raise
            except Exception:
                pass  # Inspect the page below, including explicit no-result/challenge text.
        results: list[JsonObject] = []
        for block in page.query_selector_all(str(active_profile["result_selector"]))[: limit * 2]:
            cancellation.check()
            link = block.query_selector(str(active_profile["link_selector"]))
            if link is None:
                continue
            href = _normalize_result_url(link.get_attribute("href") or "")
            if not _looks_like_public_result_url(href):
                continue
            title = _best_result_title(block, link, active_profile)
            if not title:
                title = source_download._title_from_url(href)
            snippet_el = block.query_selector(str(active_profile["snippet_selector"]))
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

    def _anchor_scan_results(self, page: Page, limit: int) -> list[JsonObject]:
        results: list[JsonObject] = []
        seen: set[str] = set()
        for link in page.query_selector_all("a[href]"):
            cancellation.check()
            href = _normalize_result_url(link.get_attribute("href") or "")
            if href in seen or not _looks_like_public_result_url(href):
                continue
            title = _element_text(link) or str(link.get_attribute("aria-label") or "").strip()
            if not title:
                title = source_download._title_from_url(href)
            if not title or _is_search_navigation_title(title):
                continue
            seen.add(href)
            results.append({"title": title, "url": href, "snippet": "", "truncated": False})
            if len(results) >= limit:
                break
        return results

    def fetch(self, url: str) -> dict[str, object]:
        cancellation.check()
        try:
            page = _search_browser_call(
                self._fetch_on_browser_thread,
                url,
                timeout=_FETCH_TOTAL_TIMEOUT_SECONDS,
                # An abandoned navigation cannot supply a later caller's capture.
                on_abandoned=self._abandon_fetch_on_browser_thread,
            )
        except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
            self._record_worker_health()
            raise
        except TimeoutError as exc:
            self._record_worker_health()
            return with_attempt(source_download._fetch_failure(url,
                f'could not load page within {_FETCH_TOTAL_TIMEOUT_SECONDS:.0f}s: {exc}'), "browser", url)
        cancellation.check()
        return with_attempt(page, "browser", url)

    def _fetch_on_browser_thread(self, url: str) -> dict[str, object]:
        reason = check_fetch_url(url)
        if reason:
            return source_download._fetch_failure(url, f'{reason}', code="policy_denied", delivery="not_attempted")
        try:
            page: Page = self._prepare_fetch_page_on_browser_thread(url)
        except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
            raise
        except Exception as exc:
            return source_download._fetch_failure(url, f'could not prepare page: {exc}', code="execution_failed", delivery="not_attempted")
        failures = _PageFailures(page)
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
                return source_download._fetch_failure(url, f'could not load page: {exc}')
            cancellation.check()
            final_url = page.url or url
            if final_url != url:
                reason = check_fetch_url(final_url)
                if reason:
                    self._discard_fetch_page_on_browser_thread(page)
                    return source_download._fetch_failure(final_url, f'{reason} (after redirect)',
                                                         code="policy_denied", delivery="confirmed")
            if response is not None:
                status = getattr(response, "status", None)
                if isinstance(status, int) and status >= 400:
                    self._discard_fetch_page_on_browser_thread(page)
                    return source_download._fetch_failure(final_url, f'could not load page: HTTP {status}',
                        code=http_failure_code(status), delivery="confirmed")
                ctype = (response.headers.get("content-type") or "").lower()
                if source_download._is_pdf_response(ctype, final_url):
                    return source_download._pdf_download_sentinel(final_url, mime_type=ctype)
                if ctype and not any(t in ctype for t in ("html", "text", "xml", "json")):
                    return source_download._fetch_failure(final_url, f'unsupported content type: {ctype}', status="skipped",
                                                         code="unsupported_content", delivery="confirmed")
            cancellation.check()
            try:
                html, text = _fetch_page_content_after_settle(page)
            except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
                raise
            except Exception as exc:
                self._discard_fetch_page_on_browser_thread(page)
                return source_download._fetch_failure(final_url, f'could not read page content: {exc}',
                                                     code="execution_failed", delivery="confirmed")
            cancellation.check()
            if _requires_login(page, text):
                self._discard_fetch_page_on_browser_thread(page)
                return source_download._fetch_failure(final_url, "login_required: page requires authentication",
                                                     code="login_required", delivery="confirmed")
            if not _usable_fetch_page_text(text):
                return self._unreadable_page_on_browser_thread(page, final_url, html, text, failures)
            truncated = len(text) > _MAX_PAGE_CHARS
            if truncated:
                text = text[:_MAX_PAGE_CHARS]
            return {"status": "ok", "url": final_url, "title": extract_title(html), "text": text, "truncated": truncated}
        except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
            self._discard_fetch_page_on_browser_thread(page)
            raise
        finally:
            failures.close()

    def _unreadable_page_on_browser_thread(
        self, page: Page, url: str, html: str, text: str, failures: _PageFailures,
    ) -> dict[str, object]:
        code, detail = failures.failure(text)
        browser_failure = source_download._fetch_failure(url,
            "page had no usable visible content after navigation: " + detail, title=extract_title(html),
            code=code, delivery="confirmed")
        self._discard_fetch_page_on_browser_thread(page)
        return browser_failure

    def _abandon_fetch_on_browser_thread(self) -> None:
        if self.headless:
            self._close_on_browser_thread()
        else:
            self._discard_fetch_page_on_browser_thread(self._fetch_page)

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
        session = self._session
        try:
            if (session is not None and session.process is None and not session.owns_browser
                    and not session.owns_context):
                pages = {id(page): page for page in (self._fetch_page, self._search_page)
                         if page is not None and page is not session.page}
                for owned_page in pages.values():
                    self._discard_page_on_browser_thread(owned_page)
                self._fetch_page = None
                if self._search_page is not session.page:
                    self._search_page = None
            seen_pages: set[int] = set()
            for page in (self._fetch_page, self._search_page):
                if page is None or id(page) in seen_pages:
                    continue
                seen_pages.add(id(page))
                self._release_page_guard_on_browser_thread(page)
            if self._session is not None:
                if self.headless:
                    self._session.page.context.unroute_all(behavior="wait")
                self._session.close()
        finally:
            self._fetch_page = None
            self._search_page = None
            self._session = None

    def _release_page_guard_on_browser_thread(self, page: Page | None) -> None:
        if page is None:
            return
        try:
            # Drain handlers before pw.stop() disconnects their interception updates.
            page.unroute_all(behavior="wait")
            setattr(page, _PAGE_GUARD_ATTRIBUTE, False)
        except Exception:
            pass

    def _discard_page_on_browser_thread(self, page: Page | None) -> None:
        if page is None:
            return
        self._release_page_guard_on_browser_thread(page)
        with contextlib.suppress(Exception):
            page.close()

    def _discard_fetch_page_on_browser_thread(self, page: Page | None) -> None:
        self._discard_page_on_browser_thread(page)
        if page is self._fetch_page:
            self._fetch_page = None

    def _discard_search_page_on_browser_thread(self, page: Page | None) -> None:
        session = self._session
        if (session is not None and page is session.page and not session.owns_page
                and not session.owns_browser and session.process is None):
            self._release_page_guard_on_browser_thread(page)
            self._search_page = None
            return
        self._discard_page_on_browser_thread(page)
        if page is self._search_page:
            self._search_page = None


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


def _fetch_page_content_after_settle(page: Page) -> tuple[str, str]:
    html = _page_content_after_navigation(page)
    text = extract_text(html)
    if _usable_fetch_page_text(text) and not _application_content_pending(page):
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
        if _usable_fetch_page_text(text) and not _application_content_pending(page):
            return html, text
    if _application_content_pending(page):
        return html, ""  # Navigation/footer alone cannot stand in for an unloaded application.
    return best_html, best_text


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


def _content_retryable(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        "page is navigating" in text
        or "navigating and changing the content" in text
        or "execution context was destroyed" in text
    )


def _search_host(profile: JsonObject) -> str:
    try:
        return (urlparse(str(profile.get("search_url") or "")).hostname or "").lower()
    except Exception:
        return ""


def _page_url(page: Page) -> str:
    try:
        value = page.url
    except Exception:
        return ""
    return value if isinstance(value, str) else ""


def _search_engine_host(profile: JsonObject) -> str:
    try:
        return (urlparse(str(profile.get("search_url") or "")).hostname or "").lower().removeprefix("www.")
    except Exception:
        return ""


def _url_host(url: str) -> str:
    try:
        return (urlparse(url or "").hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""


def _same_host(url: str, expected_host: str) -> bool:
    try:
        actual = (urlparse(url or "").hostname or "").lower().removeprefix("www.")
    except ValueError:
        return False
    expected = (expected_host or "").lower().removeprefix("www.")
    return bool(actual and expected and (actual == expected or actual.endswith("." + expected)))


def _search_page_body_text(page: Page) -> str:
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


def _set_navigation_timeout(page: Page, timeout_ms: int) -> None:
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


def _element_text(element: ElementHandle | None) -> str:
    return _clean_space(_element_raw_text(element))


def _element_raw_text(element: ElementHandle | None) -> str:
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


def _best_result_title(block: ElementHandle, link: ElementHandle, profile: JsonObject) -> str:
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
            text = _element_text(block.query_selector(str(selector)))
        except Exception:
            text = ""
        if text:
            return text
    for line in _element_raw_text(block).splitlines():
        line = line.strip()
        if line and not line.lower().startswith(("http://", "https://")):
            return _clean_space(line)
    return ""


def _snippet_from_block(block: ElementHandle, title: str) -> str:
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


def _normalize_result_url(href: str) -> str:
    raw = (href or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        return raw
    target = _search_redirect_target(parsed)
    return target or raw


def _is_search_redirect(parsed: ParseResult) -> bool:
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    params = parse_qs(parsed.query or "")
    if (host == "bing.com" or host.endswith(".bing.com")) and path.startswith("/ck/"):
        return True
    return bool(
        (host == "duckduckgo.com" or host.endswith(".duckduckgo.com"))
        and any(key in params for key in ("uddg", "u")))


def _search_redirect_target(parsed: ParseResult) -> str:
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
    raw = unquote((value or "").strip())
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


def _is_search_navigation_title(title: str) -> bool:
    lower = title.lower()
    return lower in {"next", "previous", "more", "search", "bing", "duckduckgo"}
