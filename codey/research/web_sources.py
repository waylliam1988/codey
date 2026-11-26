"""Shared browser search and HTTP-first document acquisition for Chat and Research."""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from codey.research.connector_search import ScholarlySources
from codey.research.search_api import SearchApi
from codey.research.search_contracts import SearchOutcome, SearchState, failed_search
from codey.research.search_profiles import SearchProfile, load_profiles
from codey.research.search_provenance import SearchAttempt
from codey.research.source_acquisition import combine_acquisitions, with_attempt
from codey.research.source_download import fetch_http_document, finish_pdf_download
from codey.runtime.core import cancellation


@dataclass(frozen=True)
class BrowserSessionConfig:
    """Explicit browser-session policy for HTTP fallback rendering."""

    cdp_port: int = 0
    profile_dir: Path | None = None
    require_existing: bool = False
    bring_to_front: bool = False


class BrowserSource(Protocol):
    def search(self, query: str, *, limit: int, profile: SearchProfile, deadline: float) -> SearchOutcome: ...
    def fetch(self, url: str) -> dict[str, object]: ...
    def close(self) -> None: ...


def _background_browser(config: BrowserSessionConfig | None = None, *,
                        profiles: tuple[SearchProfile, ...] | None = None) -> BrowserSource:
    from codey.automation.browser_contracts import BrowserConfig
    from codey.automation.browser_session import BrowserSession
    from codey.research.task_browser_source import TaskBrowserSource

    settings = config or BrowserSessionConfig()
    return TaskBrowserSource(BrowserSession(BrowserConfig(headless=False,
        cdp_port=settings.cdp_port, profile_dir=str(settings.profile_dir) if settings.profile_dir else "",
        isolated=not bool(settings.cdp_port), launch=not settings.require_existing, bring_to_front=settings.bring_to_front)),
        profiles=profiles, owns_browser=True)


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
        engine: str | None = None,
        api: SearchApi | None = None,
        scholarly: ScholarlySources | None = None,
    ) -> None:
        self._browser_factory = browser_factory
        self.browser_session = browser_session
        self._browser: BrowserSource | None = None
        profiles = load_profiles()
        self.profiles = tuple(profile for profile in profiles if engine is None or profile.name == engine)
        if not self.profiles:
            raise ValueError("unknown search engine: " + str(engine))
        self.api = api if api is not None else SearchApi.configured()
        self.scholarly = scholarly
        self.last_outcome: SearchOutcome | None = None

    def bind_browser_source(self, browser: BrowserSource) -> None:
        if self._browser is not None and self._browser is not browser:
            self._browser.close()
        self._browser = browser

    def _browser_source(self) -> BrowserSource:
        if self._browser is None:
            if self._browser_factory is _background_browser:
                self._browser = _background_browser(self.browser_session, profiles=self.profiles)
            else:
                self._browser = self._browser_factory()
        return self._browser

    @property
    def bound_browser_source(self) -> BrowserSource | None:
        """Inspect ownership without constructing a second session during binding."""
        return self._browser

    @property
    def browser_source(self) -> BrowserSource:
        """Share the acquisition browser with task-owned interaction resources."""
        return self._browser_source()

    def search(self, query: str, *, limit: int = 8) -> SearchOutcome:
        query = query.strip()
        if not query:
            raise ValueError("search needs a non-empty query")
        cancellation.check()
        limit = max(1, min(limit, 8))
        deadline = min(time.monotonic() + 30.0, cancellation.current_deadline() or float("inf"))
        attempts: tuple[SearchAttempt, ...] = ()
        self.last_outcome = None
        supplement = self.scholarly.search(query, limit=limit, deadline=deadline) if self.scholarly is not None else None
        if supplement is not None:
            attempts += supplement.attempts
        cancellation.check()
        outcome = failed_search(query, "search", "search_unavailable")
        if self.api is not None:
            outcome = self.api.search(query, limit=limit, timeout=min(6.0, max(0.001, deadline - time.monotonic())))
            attempts += outcome.attempts
        if self.api is None or outcome.state == SearchState.FAILED:
            for index, profile in enumerate(self.profiles):
                cancellation.check()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                engine_deadline = time.monotonic() + remaining / (len(self.profiles) - index)
                outcome = self._browser_source().search(query, limit=limit, profile=profile, deadline=engine_deadline)
                attempts += outcome.attempts
                if outcome.state != SearchState.FAILED:
                    break
        if supplement is not None and supplement.hits:
            seen = {hit.url for hit in supplement.hits}
            hits = (*supplement.hits, *(hit for hit in outcome.hits if hit.url not in seen))[:limit]
            outcome = SearchOutcome(SearchState.HITS, hits)
        cancellation.check()
        self.last_outcome = replace(outcome, attempts=attempts or outcome.attempts)
        return self.last_outcome

    @property
    def last_search_errors(self) -> list[dict[str, str]]:
        return self.last_outcome.errors if self.last_outcome is not None else []

    @property
    def last_search_failure(self) -> dict[str, str]:
        errors = self.last_search_errors
        return errors[-1] if errors else {}

    @property
    def search_attempts(self) -> tuple[SearchAttempt, ...]:
        return self.last_outcome.attempts if self.last_outcome is not None else ()

    def fetch(self, url: str) -> dict[str, object]:
        with cancellation.deadline_scope(time.monotonic() + 30.0):
            connector = self.scholarly.fetch(url) if self.scholarly is not None else None
            if connector is not None and connector.get("status") == "ok":
                return connector
            http_page = fetch_http_document(url)
            if connector is not None:
                http_page = combine_acquisitions(connector, http_page)
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
