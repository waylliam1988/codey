"""Bounded HTTP search and page reading for explicit ordinary-chat lookups."""
from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from typing import Protocol
from urllib.parse import urlencode

from codey.research.connector_search import _read_url_text
from codey.research.extract import extract_text
from codey.research.source_download import fetch_http_document
from codey.runtime.core import cancellation
from codey.types.json_value import JsonObject, JsonValue


class PageRenderer(Protocol):
    def fetch(self, url: str) -> dict[str, object]: ...
    def close(self) -> None: ...


def _background_renderer() -> PageRenderer:
    from codey.research.browser_search import BrowserSearchProvider

    return BrowserSearchProvider(headless=True, http_fallback=False)


def _attempt(backend: str, url: str, page: dict[str, object]) -> JsonObject:
    attempt: JsonObject = {"backend": backend, "requested_url": url,
                           "final_url": str(page.get("url") or url), "status": str(page.get("status") or "error")}
    if page.get("detail"):
        attempt["detail"] = str(page["detail"])
    return attempt


class ChatWebProvider:
    name = "bounded_http"

    def __init__(self, *, renderer_factory: Callable[[], PageRenderer] = _background_renderer) -> None:
        self._renderer_factory = renderer_factory
        self._renderer: PageRenderer | None = None

    def search(self, query: str, *, limit: int = 8) -> list[JsonObject]:
        cancellation.check()
        url = "https://www.bing.com/search?" + urlencode({"format": "rss", "q": query})
        document = _read_url_text(url, timeout=12)
        try:
            root = ET.fromstring(document)
        except ET.ParseError as exc:
            raise ValueError("HTTP search did not return readable RSS results") from exc
        if root.tag != "rss" or root.find("channel") is None:
            raise ValueError("HTTP search returned an unsupported document")
        results: list[JsonObject] = []
        for item in root.findall("./channel/item")[:max(1, min(limit, 8))]:
            title, target = item.findtext("title", "").strip(), item.findtext("link", "").strip()
            if title and target:
                results.append({"title": title, "url": target, "snippet": extract_text(item.findtext("description", ""))})
        cancellation.check()
        return results

    def fetch(self, url: str) -> dict[str, object]:
        with cancellation.deadline_scope(time.monotonic() + 30.0):
            page = fetch_http_document(url)
            cancellation.check()
            attempts: list[JsonValue] = [_attempt("http", url, page)]
            if page.get("render_required") is True:
                if self._renderer is None:
                    self._renderer = self._renderer_factory()
                page = self._renderer.fetch(url)
                cancellation.check()
                attempts.append(_attempt("browser", url, page))
            return {**page, "acquisition": {"version": 1, "attempts": attempts}}

    def close(self) -> None:
        if self._renderer is not None:
            self._renderer.close()
            self._renderer = None
