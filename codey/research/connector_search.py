"""Connector-aware Research search provider.

This wrapper keeps the controller surface semantic. The model calls
open_result/reopen_source/open_hit for selected results, while runtime
primitives still use open_url to fetch PubMed/arXiv abstracts through the
connector boundary before falling back to the browser provider.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from urllib.parse import urlencode, urlparse

from codey import cancellation
from codey.research.connector_domains import preferred_connector_ids
from codey.research.identity import clip
from codey.research.source_connectors import (
    CONNECTOR_AVAILABLE_STATUSES,
    MAX_CONNECTOR_HITS,
    FetchedSource,
    SafeConnectorQuery,
    SourceConnectorRegistry,
    SourceConnectorResult,
    SourceHit,
    built_in_connector_registry,
    fetch_recorded_hit,
    is_valid_arxiv_id,
    is_valid_pubmed_id,
    parse_arxiv_atom_fixture,
    parse_pubmed_fixture,
    safe_connector_query,
)
from codey.research.shape import bounded_limit as _bounded_limit, connector_id as _connector_id
from codey.research.url_policy import check_fetch_url


CONNECTOR_SEARCH_IDS = ("pubmed", "arxiv")
CONNECTOR_RESULT_LIMIT = 2
CONNECTOR_TIMEOUT_SECONDS = 4
CONNECTOR_MIN_TIMEOUT_SECONDS = 0.2
CONNECTOR_SEARCH_BUDGET_SECONDS = 6.0
_PUBMED_SEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
_PUBMED_FETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
_ARXIV_SEARCH_URL = "https://export.arxiv.org/api/query"
_PUBMED_TOOL_NAME = "research_connector"
_CONNECTOR_USER_AGENT = "Research Connector"


class ConnectorAwareSearchProvider:
    """Wrap a browser provider with PubMed/arXiv connector search/fetch."""

    def __init__(
        self,
        base_provider,
        *,
        registry: SourceConnectorRegistry | None = None,
        connector_ids: tuple[str, ...] = CONNECTOR_SEARCH_IDS,
        connector_limit: int = CONNECTOR_RESULT_LIMIT,
        timeout: int = CONNECTOR_TIMEOUT_SECONDS,
        connector_budget_seconds: float = CONNECTOR_SEARCH_BUDGET_SECONDS,
        rate_limit: bool = True,
    ) -> None:
        self.base_provider = base_provider
        self.registry = registry or built_in_connector_registry()
        self.connector_ids = tuple(
            item
            for item in connector_ids
            if self._connector_available(item, capability="search_supported")
        )
        self.connector_limit = _bounded_limit(
            connector_limit,
            default=CONNECTOR_RESULT_LIMIT,
            upper=MAX_CONNECTOR_HITS,
        )
        self.timeout = _bounded_timeout(timeout)
        self.connector_budget_seconds = max(0.0, float(connector_budget_seconds or 0.0))
        self.rate_limit = bool(rate_limit)
        self.name = "connector_aware/" + str(getattr(base_provider, "name", "browser"))
        self._hits_by_url: dict[str, SourceHit] = {}
        self._last_request_at: dict[str, float] = {}
        self._search_deadline: float | None = None
        self.last_connector_errors: list[dict[str, str]] = []

    def search(self, query: str, limit: int = 8) -> list[dict]:
        query = " ".join(str(query or "").split())
        if not query:
            return []
        result_limit = _bounded_limit(limit, default=8, upper=MAX_CONNECTOR_HITS)
        base_results = self._base_search(query, limit=result_limit)
        connector_results = self._connector_search_results(
            query,
            limit=min(self.connector_limit, result_limit),
        )
        return _merge_results(connector_results, base_results, limit=result_limit)

    def fetch(self, url: str) -> dict:
        target = str(url or "").strip()
        hit = self._hits_by_url.get(_canonical_url_key(target))
        if hit is None:
            try:
                hit = self._fetchable_hit_for_url(target)
            except cancellation.TaskCancelled:
                raise
            except Exception as exc:
                self._record_error(_connector_id_for_url(target), "fetch_lookup", exc)
                hit = None
        if hit is not None:
            try:
                return _page_from_fetched(fetch_recorded_hit(hit))
            except Exception as exc:
                self._record_error(hit.connector_id, "fetch", exc)
        return self.base_provider.fetch(target)

    def close(self) -> None:
        close = getattr(self.base_provider, "close", None)
        if callable(close):
            close()

    def _base_search(self, query: str, *, limit: int) -> list[dict]:
        try:
            return list(self.base_provider.search(query, limit=limit))
        except cancellation.TaskCancelled:
            raise
        except Exception as exc:
            self._record_error("browser", "search", exc)
            return []

    def _connector_search_results(self, query: str, *, limit: int) -> list[dict]:
        results: list[dict] = []
        safe_query = safe_connector_query(query, limit=12)
        if not safe_query.terms:
            self._record_skip("connector", "search", safe_query.skip_reason or "connector_query_empty")
            return results
        previous_deadline = self._search_deadline
        self._search_deadline = (
            time.monotonic() + self.connector_budget_seconds
            if self.connector_budget_seconds > 0
            else None
        )
        try:
            for connector_id in _preferred_connectors(safe_query, self.connector_ids):
                if self._connector_budget_exhausted():
                    break
                remaining = limit - len(results)
                if remaining <= 0:
                    break
                connector_result = self._search_connector(connector_id, safe_query, remaining)
                for hit in connector_result.hits:
                    if hit.canonical_url:
                        self._hits_by_url[_canonical_url_key(hit.canonical_url)] = hit
                    row = _result_from_hit(hit)
                    if row:
                        results.append(row)
                    if len(results) >= limit:
                        break
            return results
        finally:
            self._search_deadline = previous_deadline

    def _search_connector(
        self,
        connector_id: str,
        safe_query: SafeConnectorQuery,
        limit: int,
    ) -> SourceConnectorResult:
        if not self._connector_available(connector_id, capability="search_supported"):
            return SourceConnectorResult(connector_id=connector_id)
        try:
            if connector_id == "pubmed":
                return self._search_pubmed(safe_query, limit)
            if connector_id == "arxiv":
                return self._search_arxiv(safe_query, limit)
        except cancellation.TaskCancelled:
            raise
        except Exception as exc:
            self._record_error(connector_id, "search", exc)
        return SourceConnectorResult(connector_id=connector_id)

    def _search_pubmed(self, safe_query: SafeConnectorQuery, limit: int) -> SourceConnectorResult:
        connector_query = _connector_query(safe_query.terms, "pubmed")
        if not connector_query:
            return SourceConnectorResult(connector_id="pubmed")
        ids = self._pubmed_ids(connector_query, limit=limit)
        return self._pubmed_fetch_ids(ids, query=connector_query, limit=limit)

    def _pubmed_ids(self, query: str, *, limit: int) -> tuple[str, ...]:
        params = urlencode({
            "db": "pubmed",
            "term": query,
            "retmax": str(_bounded_limit(limit, default=self.connector_limit, upper=MAX_CONNECTOR_HITS)),
            "retmode": "json",
            "tool": _PUBMED_TOOL_NAME,
        })
        payload = json.loads(self._read_connector_url("pubmed", f"{_PUBMED_SEARCH_URL}?{params}"))
        raw_ids = payload.get("esearchresult", {}).get("idlist", [])
        return tuple(str(item).strip() for item in raw_ids if is_valid_pubmed_id(item))

    def _pubmed_fetch_ids(
        self,
        ids: tuple[str, ...],
        *,
        query: str = "",
        limit: int = CONNECTOR_RESULT_LIMIT,
    ) -> SourceConnectorResult:
        clean_ids = tuple(item for item in ids if is_valid_pubmed_id(item))[:MAX_CONNECTOR_HITS]
        if not clean_ids:
            return SourceConnectorResult(connector_id="pubmed")
        params = urlencode({
            "db": "pubmed",
            "id": ",".join(clean_ids),
            "retmode": "xml",
            "tool": _PUBMED_TOOL_NAME,
        })
        xml_text = self._read_connector_url("pubmed", f"{_PUBMED_FETCH_URL}?{params}")
        return parse_pubmed_fixture(xml_text, query=query, limit=limit)

    def _search_arxiv(self, safe_query: SafeConnectorQuery, limit: int) -> SourceConnectorResult:
        connector_query = _connector_query(safe_query.terms, "arxiv")
        if not connector_query:
            return SourceConnectorResult(connector_id="arxiv")
        params = urlencode({
            "search_query": "all:" + connector_query,
            "start": "0",
            "max_results": str(_bounded_limit(limit, default=self.connector_limit, upper=MAX_CONNECTOR_HITS)),
            "sortBy": "relevance",
            "sortOrder": "descending",
        })
        xml_text = self._read_connector_url("arxiv", f"{_ARXIV_SEARCH_URL}?{params}")
        return parse_arxiv_atom_fixture(xml_text, query=connector_query, limit=limit)

    def _fetchable_hit_for_url(self, url: str) -> SourceHit | None:
        if _is_pubmed_url(url):
            if not self._connector_available("pubmed", capability="fetch_supported"):
                return None
            pmid = _pubmed_id_from_url(url)
            result = self._pubmed_fetch_ids((pmid,), limit=1) if pmid else SourceConnectorResult("pubmed")
            return result.hits[0] if result.hits else None
        if _is_arxiv_url(url):
            if not self._connector_available("arxiv", capability="fetch_supported"):
                return None
            arxiv_id = _arxiv_id_from_url(url)
            result = self._arxiv_fetch_id(arxiv_id) if arxiv_id else SourceConnectorResult("arxiv")
            return result.hits[0] if result.hits else None
        return None

    def _arxiv_fetch_id(self, arxiv_id: str) -> SourceConnectorResult:
        if not is_valid_arxiv_id(arxiv_id):
            return SourceConnectorResult(connector_id="arxiv")
        params = urlencode({"id_list": arxiv_id, "max_results": "1"})
        xml_text = self._read_connector_url("arxiv", f"{_ARXIV_SEARCH_URL}?{params}")
        return parse_arxiv_atom_fixture(xml_text, limit=1)

    def _read_connector_url(self, connector_id: str, url: str) -> str:
        self._wait_for_rate_limit(connector_id)
        return _read_url_text(url, timeout=self._request_timeout())

    def _request_timeout(self) -> float:
        if self._search_deadline is None:
            return float(self.timeout)
        remaining = self._search_deadline - time.monotonic()
        if remaining <= CONNECTOR_MIN_TIMEOUT_SECONDS:
            raise TimeoutError("connector search budget exhausted")
        return min(self.timeout, remaining)

    def _connector_budget_exhausted(self) -> bool:
        return self._search_deadline is not None and time.monotonic() >= self._search_deadline

    def _wait_for_rate_limit(self, connector_id: str) -> None:
        if not self.rate_limit:
            return
        try:
            delay = float(self.registry.get(connector_id).rate_limit_seconds or 0.0)
        except (KeyError, TypeError, ValueError):
            delay = 0.0
        if delay <= 0:
            return
        now = time.monotonic()
        last = self._last_request_at.get(connector_id, 0.0)
        remaining = delay - (now - last)
        if remaining > 0:
            if self._search_deadline is not None and now + remaining >= self._search_deadline:
                raise TimeoutError("connector search budget exhausted")
            cancellation.wait(remaining)
        self._last_request_at[connector_id] = time.monotonic()

    def _record_error(self, connector_id: str, action: str, exc: object) -> None:
        self.last_connector_errors.append({
            "connector_id": _connector_id(connector_id),
            "action": _connector_id(action),
            "error": clip(type(exc).__name__ or str(exc), 80),
        })
        del self.last_connector_errors[:-8]

    def _record_skip(self, connector_id: str, action: str, reason: str) -> None:
        self.last_connector_errors.append({
            "connector_id": _connector_id(connector_id),
            "action": _connector_id(action),
            "error": _connector_id(reason),
        })
        del self.last_connector_errors[:-8]

    def _connector_available(self, connector_id: object, *, capability: str) -> bool:
        connector = _connector_id(connector_id)
        if connector not in CONNECTOR_SEARCH_IDS:
            return False
        try:
            spec = self.registry.get(connector)
        except KeyError:
            return False
        return (
            spec.status in CONNECTOR_AVAILABLE_STATUSES
            and bool(spec.shipped)
            and bool(getattr(spec, capability, False))
        )


def _preferred_connectors(safe_query: SafeConnectorQuery, connector_ids: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        item
        for item in preferred_connector_ids(
            safe_query.terms,
            available_ids=connector_ids,
        )
        if item in connector_ids
    )


def _connector_query(safe_terms: tuple[str, ...], connector_id: str) -> str:
    blocked = {
        "cited",
        "concise",
        "current",
        "exact",
        "evidence",
        "find",
        "opened",
        "report",
        "research",
        "save",
        "search",
        "source",
        "sources",
        "use",
    }
    if connector_id == "pubmed":
        blocked.update({"pubmed", "biomedical", "literature"})
    elif connector_id == "arxiv":
        blocked.update({"arxiv", "preprint", "preprints", "paper", "papers"})
    terms = [
        item
        for item in safe_terms[:MAX_CONNECTOR_HITS]
        if item.strip().casefold().strip(".,:;()[]{}") not in blocked
    ]
    return " ".join(terms)


def _result_from_hit(hit: SourceHit) -> dict:
    if not hit.canonical_url:
        return {}
    label = "PubMed" if hit.connector_id == "pubmed" else "arXiv"
    return {
        "title": f"{label}: {clip(hit.title, 140)}" if hit.title else label,
        "url": hit.canonical_url,
        "snippet": clip(hit.snippet, 320),
    }


def _page_from_fetched(fetched: FetchedSource) -> dict:
    document = fetched.to_source_document()
    return {
        "url": document.final_url,
        "title": document.title,
        "text": document.text,
        "content_kind": document.content_kind,
        "mime_type": document.mime_type,
        "truncated": document.truncated,
    }


def _merge_results(connector_results: list[dict], base_results: list[dict], *, limit: int) -> list[dict]:
    merged: list[dict] = []
    seen_connector: set[str] = set()
    seen_base: set[str] = set()
    for row in connector_results:
        url = str(row.get("url") or "").strip()
        if not url:
            continue
        key = _canonical_url_key(url)
        if key in seen_connector:
            continue
        seen_connector.add(key)
        merged.append({
            "title": str(row.get("title") or "").strip(),
            "url": url,
            "snippet": str(row.get("snippet") or "").strip(),
        })
        if len(merged) >= limit:
            break
    for row in base_results:
        url = str(row.get("url") or "").strip()
        if not url:
            continue
        if _canonical_url_key(url) in seen_connector:
            continue
        key = _full_url_key(url)
        if key in seen_base:
            continue
        seen_base.add(key)
        merged.append({
            "title": str(row.get("title") or "").strip(),
            "url": url,
            "snippet": str(row.get("snippet") or "").strip(),
        })
        if len(merged) >= limit:
            break
    return merged


def _read_url_text(url: str, *, timeout: float) -> str:
    reason = check_fetch_url(url, resolve=False)
    if reason:
        raise ValueError(reason)
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json, application/xml, text/xml, */*;q=0.8",
            "User-Agent": _CONNECTOR_USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read(1024 * 1024)
            charset = response.headers.get_content_charset() or "utf-8"
    except urllib.error.URLError as exc:
        raise ValueError(f"connector request failed: {exc}") from exc
    return data.decode(charset, errors="replace")


def _is_pubmed_url(url: str) -> bool:
    parsed = _parsed(url)
    return (parsed.hostname or "").lower().removeprefix("www.") == "pubmed.ncbi.nlm.nih.gov"


def _is_arxiv_url(url: str) -> bool:
    parsed = _parsed(url)
    return (parsed.hostname or "").lower().removeprefix("www.") == "arxiv.org" and parsed.path.startswith("/abs/")


def _pubmed_id_from_url(url: str) -> str:
    parsed = _parsed(url)
    parts = [item for item in parsed.path.split("/") if item]
    return parts[0] if parts and parts[0].isdigit() else ""


def _arxiv_id_from_url(url: str) -> str:
    parsed = _parsed(url)
    if not parsed.path.startswith("/abs/"):
        return ""
    arxiv_id = parsed.path.removeprefix("/abs/").strip("/")
    return arxiv_id if is_valid_arxiv_id(arxiv_id) else ""


def _parsed(url: str):
    try:
        return urlparse(str(url or "").strip())
    except ValueError:
        return urlparse("")


def _canonical_url_key(url: str) -> str:
    parsed = _parsed(url)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    path = (parsed.path or "").rstrip("/")
    return f"{parsed.scheme.lower()}://{host}{path}"


def _full_url_key(url: str) -> str:
    parsed = _parsed(url)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    path = (parsed.path or "").rstrip("/")
    query = ("?" + parsed.query) if parsed.query else ""
    return f"{parsed.scheme.lower()}://{host}{path}{query}"


def _connector_id_for_url(url: str) -> str:
    if _is_pubmed_url(url):
        return "pubmed"
    if _is_arxiv_url(url):
        return "arxiv"
    return "connector"


def _bounded_timeout(value: object) -> float:
    if isinstance(value, bool):
        return float(CONNECTOR_TIMEOUT_SECONDS)
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = float(CONNECTOR_TIMEOUT_SECONDS)
    return max(CONNECTOR_MIN_TIMEOUT_SECONDS, min(parsed, float(CONNECTOR_TIMEOUT_SECONDS)))


__all__ = [
    "CONNECTOR_RESULT_LIMIT",
    "CONNECTOR_SEARCH_IDS",
    "ConnectorAwareSearchProvider",
]
