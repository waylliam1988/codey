"""Bounded HTTP search and page reading for explicit ordinary-chat lookups."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from urllib.parse import urlencode

from codey.research.connector_search import _read_url_text
from codey.research.extract import extract_text
from codey.research.source_download import fetch_http_document
from codey.runtime.core import cancellation
from codey.types.json_value import JsonObject


class ChatWebProvider:
    name = "bounded_http"

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
        return fetch_http_document(url)

    def close(self) -> None:
        pass
