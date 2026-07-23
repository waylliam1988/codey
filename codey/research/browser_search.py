"""Zero-key web search and page fetch for Research."""

from __future__ import annotations

import base64
import binascii
import json
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

from codey import cancellation, browser_worker
from codey.browser import DEFAULT_PORT, DEFAULT_PROFILE, open_chat_page
from codey.research.extract import extract_text, extract_title
from codey.research.url_policy import check_fetch_url

_PROFILES_PATH = Path(__file__).with_name("search_profiles.json")
_NAV_TIMEOUT_MS = 20_000
_MAX_PAGE_CHARS = 200_000


def load_profiles() -> dict:
    try:
        return json.loads(_PROFILES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"default_engine": "bing", "engines": {}}


class BrowserSearchProvider:
    name = "browser"

    def __init__(
        self,
        *,
        engine: str | None = None,
        profile_dir: Path | None = None,
        cdp_port: int = DEFAULT_PORT,
        browser_path: str | None = None,
        launch: bool = True,
    ) -> None:
        profiles = load_profiles()
        self.engine = engine or profiles.get("default_engine", "bing")
        self._profile = profiles.get("engines", {}).get(self.engine)
        if not self._profile:
            raise ValueError(f"unknown search engine: {self.engine}")
        self._reuse_url_contains = _search_host(self._profile)
        self.profile_dir = Path(profile_dir) if profile_dir else DEFAULT_PROFILE
        self.cdp_port = int(cdp_port)
        self.browser_path = browser_path
        self.launch = launch
        self._session = None
        self._search_page = None
        self._fetch_page = None

    def _ensure_session_on_browser_thread(self, *, reuse_url_contains: str = ""):
        if self._session is not None:
            return self._session
        self._session = open_chat_page(
            "about:blank" if reuse_url_contains else "about:blank",
            reuse_url_contains or "",
            port=self.cdp_port,
            profile=self.profile_dir,
            open_if_missing=self.launch,
            bring_to_front=False,
            isolated=False,
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
            self._bring_to_front_on_browser_thread(self._search_page)
        return self._prepare_page_on_browser_thread(self._search_page)

    def _ensure_fetch_page_on_browser_thread(self, url: str):
        session = self._ensure_session_on_browser_thread()
        if self._page_closed_on_browser_thread(self._fetch_page):
            context = self._page_context_on_browser_thread(self._search_page or session.page)
            self._fetch_page = context.new_page()
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
            blocked = bool(check_fetch_url(route.request.url))
        except Exception:
            blocked = True
        try:
            route.abort() if blocked else route.continue_()
        except Exception:
            pass

    def search(self, query: str, limit: int = 8) -> list[dict]:
        cancellation.check()
        results = browser_worker.call(self._search_on_browser_thread, query, limit)
        cancellation.check()
        return results

    def _search_on_browser_thread(self, query: str, limit: int) -> list[dict]:
        page = self._ensure_search_page_on_browser_thread()
        url = self._profile["search_url"].format(query=quote_plus(query))
        page.goto(url, wait_until="domcontentloaded")
        results: list[dict] = []
        for block in page.query_selector_all(self._profile["result_selector"])[: limit * 2]:
            link = block.query_selector(self._profile["link_selector"])
            if link is None:
                continue
            href = _normalize_result_url(link.get_attribute("href") or "")
            if not _looks_like_public_result_url(href):
                continue
            title = _best_result_title(block, link, self._profile)
            if not title:
                title = _title_from_url(href)
            snippet_el = block.query_selector(self._profile["snippet_selector"])
            snippet = _element_text(snippet_el) if snippet_el else _snippet_from_block(block, title)
            results.append({"title": title, "url": href, "snippet": snippet})
            if len(results) >= limit:
                break
        if not results:
            results = self._fallback_results(page, limit)
        return results

    def _fallback_results(self, page, limit: int) -> list[dict]:
        results: list[dict] = []
        seen: set[str] = set()
        for link in page.query_selector_all("a[href]"):
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
        page = browser_worker.call(self._fetch_on_browser_thread, url)
        cancellation.check()
        return page

    def _fetch_on_browser_thread(self, url: str) -> dict:
        reason = check_fetch_url(url)
        if reason:
            return {"url": url, "title": "", "text": f"ERROR: {reason}", "truncated": False}
        page = self._ensure_fetch_page_on_browser_thread(url)
        try:
            response = page.goto(url, wait_until="domcontentloaded")
        except Exception as exc:
            return {"url": url, "title": "", "text": f"ERROR: could not load page: {exc}", "truncated": False}
        final_url = page.url or url
        if final_url != url:
            reason = check_fetch_url(final_url)
            if reason:
                return {"url": final_url, "title": "", "text": f"ERROR: {reason} (after redirect)", "truncated": False}
        if response is not None:
            ctype = (response.headers.get("content-type") or "").lower()
            if ctype and not any(t in ctype for t in ("html", "text", "xml", "json")):
                return {"url": final_url, "title": "", "text": f"ERROR: unsupported content type: {ctype}", "truncated": False}
        html = page.content()
        text = extract_text(html)
        truncated = len(text) > _MAX_PAGE_CHARS
        if truncated:
            text = text[:_MAX_PAGE_CHARS]
        return {"url": final_url, "title": extract_title(html), "text": text, "truncated": truncated}

    def close(self) -> None:
        if self._session is None:
            return
        browser_worker.call(self._close_on_browser_thread)

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


def _search_host(profile: dict) -> str:
    try:
        return urlparse(str(profile.get("search_url") or "")).netloc
    except Exception:
        return ""


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
