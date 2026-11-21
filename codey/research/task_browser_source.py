"""Source acquisition borrows the task's browser, never a second driver."""
from __future__ import annotations

import json
import time
from dataclasses import replace
from urllib.parse import quote_plus, urlparse

from codey.automation.browser_session import BrowserSession
from codey.research import source_download
from codey.research.browser_page import validate_browser_capture
from codey.research.browser_search import (
    SearchUnavailableError,
    _is_search_navigation_title,
    _looks_like_public_result_url,
    _normalize_result_url,
    _same_host,
    _search_engine_host,
    _search_failure_detail,
    _search_failure_kind,
    _search_page_failure_kind,
    _usable_search_page_text,
    load_profiles,
)
from codey.research.search_provenance import (
    EXCLUDED_SEARCH_REGIONS,
    SearchAttempt,
    filter_site_results,
    query_consistency,
)
from codey.research.source_acquisition import with_attempt
from codey.runtime.core import cancellation
from codey.types.json_value import JsonObject, decode_json_object, require_json_string

SEARCH_RESULTS = r"""({profile,limit,excluded}) => {
    const clean = node => (node?.innerText || node?.textContent || '').replace(/\s+/g,' ').trim();
    const rows = [];
    for (const block of Array.from(document.querySelectorAll(profile.result_selector)).slice(0,limit*2)) {
        if (block.closest(excluded)) continue;
        const link = block.querySelector(profile.link_selector);
        if (!link) continue;
        rows.push({url:link.href, title:clean(link) || clean(block.querySelector(profile.title_selector)),
            snippet:clean(block.querySelector(profile.snippet_selector)) || clean(block)});
    }
    const anchors = Array.from(document.querySelectorAll('a[href]')).filter(link =>
        link.getClientRects().length && !link.closest(excluded) &&
        link.closest('main,[role="main"],#b_results,#links,.results')).slice(0,500)
        .map(link=>({url:link.href,title:clean(link)||link.getAttribute('aria-label')||'',snippet:''}));
    const query = document.querySelector('input[name="q"]');
    return {rows, anchors, url:location.href, query:query ? query.value : null,
        text:(document.body?.innerText||'').slice(0,12000)};
}"""


class TaskBrowserSource:
    name = "browser"

    def __init__(self, browser: BrowserSession, *, engine: str | None = None) -> None:
        self.browser = browser
        self.engine = engine
        self.last_search_errors: list[dict[str, str]] = []
        self.last_search_failure: dict[str, str] = {}
        self.search_attempts: tuple[SearchAttempt, ...] = ()

    def close(self) -> None:
        # The task resource owns the lease. Source acquisition only borrows it.
        pass

    def fetch(self, url: str) -> dict[str, object]:
        opened = self.browser.navigate(url)
        if not opened.ok:
            delivery = opened.observation.get("delivery")
            return with_attempt({"status": "error", "url": url, "detail": opened.model_text,
                "failure_code": "request_failed", "delivery": "not_attempted" if delivery == "not_delivered" else
                "confirmed" if delivery in {"delivered", "verified"} else "unconfirmed"}, "browser", url)
        deadline = time.monotonic() + 3
        while True:
            page = self.browser.capture()
            if str(page.get("text") or "").strip() or time.monotonic() >= deadline:
                break
            cancellation.wait(0.1)
        status = page.get("http_status")
        final_url = str(page.get("url") or url)
        content_type = str(page.get("content_type") or "")
        if source_download._is_pdf_response(content_type, final_url) and (type(status) is not int or status < 400):
            discovered = with_attempt(source_download._pdf_download_sentinel(final_url, mime_type=content_type), "browser", url)
            return source_download.finish_pdf_download(discovered, url)
        return with_attempt(validate_browser_capture(page), "browser", url)

    def search(self, query: str, *, limit: int = 8) -> list[JsonObject]:
        self.last_search_errors.clear()
        self.last_search_failure = {}
        self.search_attempts = self.search_attempts[:0]
        settings = load_profiles()
        profiles = decode_json_object(settings["engines"])
        preferred = self.engine or require_json_string(settings["default_engine"], "default_engine")
        engines = (preferred,) if self.engine else (preferred, *(engine for engine in profiles if engine != preferred))
        limit = max(1, min(limit, 8))
        deadline = min(time.monotonic() + 24, cancellation.current_deadline() or float("inf"))
        for index, engine in enumerate(engines):
            cancellation.check()
            if time.monotonic() >= deadline:
                break
            try:
                profile = decode_json_object(profiles[engine])
                engine_deadline = time.monotonic() + (deadline - time.monotonic()) / (len(engines) - index)
                with cancellation.deadline_scope(engine_deadline):
                    return self._search_engine(query, limit, engine, profile)
            except cancellation.TaskCancelled:
                raise
            except (ValueError, RuntimeError, TimeoutError) as exc:
                cancellation.check()
                self.last_search_failure = {"engine": engine, "failure_kind": _search_failure_kind(exc),
                    "error": _search_failure_detail(exc)[:160]}
                self.last_search_errors.append(self.last_search_failure)
                if self.search_attempts:
                    self.search_attempts = (*self.search_attempts[:-1], replace(self.search_attempts[-1],
                        failure_kind=self.last_search_failure["failure_kind"]))
        raise SearchUnavailableError(self.last_search_failure.get("failure_kind", "search_timeout"),
            engine=self.last_search_failure.get("engine", ""), detail=str(self.last_search_errors))

    def _search_engine(self, query: str, limit: int, engine: str, profile: JsonObject) -> list[JsonObject]:
        self.search_attempts += (SearchAttempt(query, engine),)
        url = require_json_string(profile["search_url"], "search_url").format(query=quote_plus(query))
        opened = self.browser.navigate(url)
        if not opened.ok:
            raise SearchUnavailableError("search_navigation_failed", engine=engine, detail=opened.model_text)
        self.browser.wait("selector", require_json_string(profile["result_selector"], "result selector"), 2500)
        observed = self.browser.evaluate("(" + SEARCH_RESULTS + ")(" + json.dumps({
            "profile": profile, "limit": limit, "excluded": EXCLUDED_SEARCH_REGIONS}) + ")")
        if not observed.ok:
            raise SearchUnavailableError("search_observation_failed", engine=engine, detail=observed.model_text)
        value = decode_json_object(observed.observation["value"])
        final = require_json_string(value["url"], "search URL")
        consistent = query_consistency(final, query)
        if value["query"] is not None and value["query"] != query:
            consistent = False
        self.search_attempts = (*self.search_attempts[:-1], replace(self.search_attempts[-1],
            final_url=final, query_consistent=consistent))
        if not _same_host(final, _search_engine_host(profile)):
            raise SearchUnavailableError("search_wrong_host", engine=engine, observed_host=urlparse(final).hostname or "")
        if consistent is not True:
            raise SearchUnavailableError("search_query_mismatch", engine=engine)
        text = require_json_string(value["text"], "search text")
        if not _usable_search_page_text(text):
            raise SearchUnavailableError(_search_page_failure_kind(text), engine=engine)
        rows = self._result_rows(value["rows"], 500)
        scope = "result_regions" if rows else "main_anchors"
        if not rows:
            rows = self._result_rows(value["anchors"], 500)
        rows, rejected = filter_site_results(query, rows)
        rows = rows[:limit]
        self.search_attempts = (*self.search_attempts[:-1], replace(self.search_attempts[-1],
            extraction_scope=scope, accepted_urls=tuple(str(row["url"]) for row in rows), rejected_urls=rejected))
        return rows

    @staticmethod
    def _result_rows(raw: object, limit: int) -> list[JsonObject]:
        if not isinstance(raw, list):
            raise ValueError("invalid search results")
        values: list[object] = raw
        results: list[JsonObject] = []
        seen: set[str] = set()
        for item in values:
            row = decode_json_object(item)
            href = _normalize_result_url(require_json_string(row["url"], "result URL"))
            title = require_json_string(row["title"], "result title") or source_download._title_from_url(href)
            if href in seen or not _looks_like_public_result_url(href) or _is_search_navigation_title(title):
                continue
            seen.add(href)
            results.append({**row, "url": href, "title": title})
            if len(results) >= limit:
                break
        return results
