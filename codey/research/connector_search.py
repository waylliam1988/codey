"""Connector-aware Research search provider.

This wrapper keeps the controller surface semantic. The model calls
open_result/reopen_source/open_hit for selected results, while runtime
primitives still use open_url to fetch PubMed/arXiv abstracts through the
connector boundary before falling back to the browser provider.
"""

from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.request
from contextlib import AbstractContextManager
from dataclasses import replace
from typing import Protocol, runtime_checkable
from urllib.parse import urlencode

from codey.policies.network import check_fetch_url
from codey.research.connector_terms import preferred_connector_ids
from codey.research.guards import bounded_limit as _bounded_limit
from codey.research.guards import connector_id as _connector_id
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
from codey.research.search_contracts import SearchHit, SearchOutcome, SearchState, complete_search
from codey.research.search_provenance import SearchAttempt
from codey.research.source_acquisition import with_attempt
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
    scholarly_metadata_hit,
    source_result_from_hits,
)
from codey.research.urls import canonical_key, host_key, parsed_url
from codey.runtime.core import cancellation
from codey.types.json_value import JsonObject, decode_json_object
from codey.utils.refs import clip, coerce_float

CONNECTOR_SEARCH_IDS = ("pubmed", "arxiv", "crossref", "openalex")
CONNECTOR_RESULT_LIMIT = 2
CONNECTOR_TIMEOUT_SECONDS = 4
CONNECTOR_MIN_TIMEOUT_SECONDS = 0.2
CONNECTOR_SEARCH_BUDGET_SECONDS = 6.0
_PUBMED_SEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
_PUBMED_FETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
_ARXIV_SEARCH_URL = "https://export.arxiv.org/api/query"
_CROSSREF_SEARCH_URL = "https://api.crossref.org/works"
_OPENALEX_SEARCH_URL = "https://api.openalex.org/works"
_PUBMED_TOOL_NAME = "research_connector"
_CONNECTOR_USER_AGENT = "Research Connector"


class ScholarlySources:
    """Scholarly search and recorded-hit fetch; backend selection belongs to WebSourceProvider."""

    def __init__(
        self,
        *,
        registry: SourceConnectorRegistry | None = None,
        connector_ids: tuple[str, ...] = CONNECTOR_SEARCH_IDS,
        connector_limit: int = CONNECTOR_RESULT_LIMIT,
        timeout: int = CONNECTOR_TIMEOUT_SECONDS,
        connector_budget_seconds: float = CONNECTOR_SEARCH_BUDGET_SECONDS,
        rate_limit: bool = True,
    ) -> None:
        self.registry = registry or built_in_connector_registry()
        self.connector_ids = tuple(
            item for item in connector_ids if self._connector_available(item, capability="search_supported")
        )
        self.connector_limit = _bounded_limit(
            connector_limit,
            default=CONNECTOR_RESULT_LIMIT,
            upper=MAX_CONNECTOR_HITS,
        )
        self.timeout = _bounded_timeout(timeout)
        self.connector_budget_seconds = max(0.0, float(connector_budget_seconds or 0.0))
        self.rate_limit = rate_limit
        self.name = "scholarly"
        self._hits_by_url: dict[str, SourceHit] = {}
        self._last_request_at: dict[str, float] = {}
        self._search_deadline: float | None = None

    def search(self, query: str, *, limit: int, deadline: float) -> SearchOutcome | None:
        safe = safe_connector_query(query, limit=12)
        preferred = _preferred_connectors(safe, self.connector_ids)
        if not preferred:
            return None
        self._search_deadline = min(deadline, time.monotonic() + self.connector_budget_seconds)
        attempts: tuple[SearchAttempt, ...] = ()
        hits: list[SearchHit] = []
        seen: set[str] = set()
        try:
            for connector in preferred:
                cancellation.check()
                if self._connector_budget_exhausted() or len(hits) >= limit:
                    break
                result = self._search_connector(connector, safe, min(limit - len(hits), self.connector_limit))
                rows = [_result_from_hit(hit) for hit in result.hits if hit.canonical_url]
                attempt = SearchAttempt(query, connector, coverage="scholarly",
                    effective_query=_connector_query(safe.terms, connector))
                if result.errors and not rows:
                    attempt = replace(attempt, failure_kind="connector_failed", detail="; ".join(result.errors), state="failed")
                    attempts += (attempt,)
                    continue
                observed = complete_search(query, rows, attempt, limit - len(hits), explicit_empty=not rows)
                attempts += observed.attempts
                for hit in observed.hits:
                    if hit.url not in seen:
                        seen.add(hit.url)
                        hits.append(hit)
                if self._connector_available(connector, capability="fetch_supported"):
                    for source_hit in result.hits:
                        if source_hit.canonical_url:
                            self._hits_by_url[canonical_key(source_hit.canonical_url)] = source_hit
            # A scholarly zero result never completes the general web query.
            return (SearchOutcome(SearchState.HITS, tuple(hits), attempts) if hits else
                    SearchOutcome(SearchState.FAILED, attempts=attempts, failure_kind="search_no_scholarly_hits"))
        finally:
            self._search_deadline = None

    def fetch(self, url: str) -> dict[str, object] | None:
        target = url.strip()
        hit = self._hits_by_url.get(canonical_key(target))
        try:
            if hit is None:
                hit = self._fetchable_hit_for_url(target)
            if hit is not None:
                return with_attempt(_page_from_fetched(fetch_recorded_hit(hit)), "connector", target)
        except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            return with_attempt({"status": "error", "failure_code": "request_failed", "detail": str(exc),
                                 "delivery": "unconfirmed"}, "connector", target)
        return None

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
            if connector_id in {"crossref", "openalex"}:
                return self._search_scholarly_metadata(connector_id, safe_query, limit)
        except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
            raise
        except Exception as exc:
            return SourceConnectorResult(connector_id=connector_id, errors=(type(exc).__name__,))
        return SourceConnectorResult(connector_id=connector_id)

    def _search_pubmed(self, safe_query: SafeConnectorQuery, limit: int) -> SourceConnectorResult:
        connector_query = _connector_query(safe_query.terms, "pubmed")
        if not connector_query:
            return SourceConnectorResult(connector_id="pubmed")
        ids = self._pubmed_ids(connector_query, limit=limit)
        return self._pubmed_fetch_ids(ids, query=connector_query, limit=limit)

    def _pubmed_ids(self, query: str, *, limit: int) -> tuple[str, ...]:
        params = urlencode(
            {
                "db": "pubmed",
                "term": query,
                "retmax": str(_bounded_limit(limit, default=self.connector_limit, upper=MAX_CONNECTOR_HITS)),
                "retmode": "json",
                "tool": _PUBMED_TOOL_NAME,
            }
        )
        payload: object = json.loads(self._read_connector_url("pubmed", f"{_PUBMED_SEARCH_URL}?{params}"))
        result = decode_json_object(decode_json_object(payload)["esearchresult"])
        raw_ids = result["idlist"]
        if not isinstance(raw_ids, list):
            raise ValueError("invalid PubMed idlist")
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
        params = urlencode(
            {
                "db": "pubmed",
                "id": ",".join(clean_ids),
                "retmode": "xml",
                "tool": _PUBMED_TOOL_NAME,
            }
        )
        xml_text = self._read_connector_url("pubmed", f"{_PUBMED_FETCH_URL}?{params}")
        return parse_pubmed_fixture(xml_text, query=query, limit=limit)

    def _search_arxiv(self, safe_query: SafeConnectorQuery, limit: int) -> SourceConnectorResult:
        connector_query = _connector_query(safe_query.terms, "arxiv")
        if not connector_query:
            return SourceConnectorResult(connector_id="arxiv")
        params = urlencode(
            {
                "search_query": "all:" + connector_query,
                "start": "0",
                "max_results": str(_bounded_limit(limit, default=self.connector_limit, upper=MAX_CONNECTOR_HITS)),
                "sortBy": "relevance",
                "sortOrder": "descending",
            }
        )
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

    def _search_scholarly_metadata(self, connector_id: str, safe_query: SafeConnectorQuery, limit: int) -> SourceConnectorResult:
        query = _connector_query(safe_query.terms, connector_id)
        if not query:
            return SourceConnectorResult(connector_id=connector_id)
        crossref = connector_id == "crossref"
        endpoint = _CROSSREF_SEARCH_URL if crossref else _OPENALEX_SEARCH_URL
        params = (dict(query=query, rows=str(limit), select="DOI,title") if crossref else
                  {"search": query, "per-page": str(limit), "select": "doi,display_name"})
        response: object = json.loads(self._read_connector_url(connector_id, f"{endpoint}?{urlencode(params)}"))
        payload = decode_json_object(response)
        rows = decode_json_object(payload.get("message")).get("items") if crossref else payload.get("results")
        if not isinstance(rows, list):
            raise ValueError("invalid scholarly metadata results")
        hits: dict[str, SourceHit] = {}
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            title = raw.get("title") if crossref else raw.get("display_name")
            if crossref:
                title = title[0] if isinstance(title, list) and title else ""
            hit = scholarly_metadata_hit(connector_id, raw.get("DOI" if crossref else "doi"), title)
            if hit is not None:
                hits.setdefault(hit.canonical_url, hit)
                if len(hits) >= limit:
                    break
        return source_result_from_hits(connector_id, query=query, hits=hits.values())

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
            and spec.shipped
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
        connector_id,
    }
    if connector_id == "pubmed":
        blocked.update({"pubmed", "biomedical", "literature"})
    elif connector_id == "arxiv":
        blocked.update({"arxiv", "preprint", "preprints", "paper", "papers"})
    terms = [
        item for item in safe_terms[:MAX_CONNECTOR_HITS] if item.strip().casefold().strip(".,:;()[]{}") not in blocked
    ]
    return " ".join(terms)


def _result_from_hit(hit: SourceHit) -> JsonObject:
    if not hit.canonical_url:
        return {}
    labels = {"pubmed": "PubMed", "arxiv": "arXiv", "crossref": "Crossref", "openalex": "OpenAlex"}
    label = labels[hit.connector_id]
    return {
        "title": f"{label}: {clip(hit.title, 140)}" if hit.title else label,
        "url": hit.canonical_url,
        "snippet": clip(hit.snippet, 320),
    }


def _page_from_fetched(fetched: FetchedSource) -> dict[str, object]:
    document = fetched.to_source_document()
    return {"status": "ok",
        "url": document.final_url,
        "title": document.title,
        "text": document.text,
        "content_kind": document.content_kind,
        "mime_type": document.mime_type,
        "truncated": document.truncated,
        "publication": document.publication,
    }


_CONNECTOR_MAX_REDIRECTS = 5
_CONNECTOR_OPENER: urllib.request.OpenerDirector = build_no_redirect_opener()


@runtime_checkable
class _ReadableResponse(Protocol):
    def read(self, size: int) -> bytes: ...


def _open_connector_url(req: urllib.request.Request, timeout: float) -> AbstractContextManager[object]:
    response: object = _CONNECTOR_OPENER.open(req, timeout=timeout)
    if not isinstance(response, AbstractContextManager):
        raise TypeError("connector response requires a context manager")
    return response





def _remaining_timeout(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("connector request timed out")
    return remaining


def _read_url_text(url: str, *, timeout: float) -> str:
    current_url = url
    deadline = time.monotonic() + max(0.0, float(timeout))
    for _ in range(_CONNECTOR_MAX_REDIRECTS + 1):
        cancellation.check()
        reason = check_fetch_url(current_url, use_cache=True)
        if reason:
            raise ValueError(reason)
        request = urllib.request.Request(
            current_url,
            headers={
                "Accept": "application/json, application/xml, text/xml, */*;q=0.8",
                "User-Agent": _CONNECTOR_USER_AGENT,
            },
        )
        try:
            with _open_connector_url(request, timeout=_remaining_timeout(deadline)) as response:
                status: object = getattr(response, "status", getattr(response, "code", 200))
                headers: object = getattr(response, "headers", None)
                if _is_redirect_status(status):
                    next_url = _redirect_target(current_url, headers)
                    if not next_url:
                        raise ValueError("redirect response missing Location header")
                    current_url = next_url
                    continue
                if not isinstance(response, _ReadableResponse):
                    raise TypeError("connector response requires a byte stream")
                data = response.read(1024 * 1024)
                return data.decode(response_charset(headers), errors="replace")
        except urllib.error.HTTPError as exc:
            if _is_redirect_status(exc.code):
                try:
                    next_url = _redirect_target(current_url, exc.headers)
                    if not next_url:
                        raise ValueError("redirect response missing Location header") from exc
                    current_url = next_url
                    continue
                finally:
                    _close_response(exc)
            _close_response(exc)
            raise ValueError(f"connector request failed: {exc}") from exc
        except urllib.error.URLError as exc:
            raise ValueError(f"connector request failed: {exc}") from exc

    raise ValueError("too many redirects")


def _is_pubmed_url(url: str) -> bool:
    return host_key(url) == "pubmed.ncbi.nlm.nih.gov"


def _is_arxiv_url(url: str) -> bool:
    parsed = parsed_url(url)
    return host_key(url) == "arxiv.org" and parsed.path.startswith("/abs/")


def _pubmed_id_from_url(url: str) -> str:
    parsed = parsed_url(url)
    parts = [item for item in parsed.path.split("/") if item]
    if not parts:
        return ""
    candidate = parts[0]
    if candidate.isascii() and candidate.isdigit():
        return candidate
    return ""


def _arxiv_id_from_url(url: str) -> str:
    parsed = parsed_url(url)
    if not parsed.path.startswith("/abs/"):
        return ""
    arxiv_id = parsed.path.removeprefix("/abs/").strip("/")
    return arxiv_id if is_valid_arxiv_id(arxiv_id) else ""


def _bounded_timeout(value: object) -> float:
    if isinstance(value, bool):
        return float(CONNECTOR_TIMEOUT_SECONDS)
    try:
        parsed = coerce_float(value, default=float(CONNECTOR_TIMEOUT_SECONDS))
    except (TypeError, ValueError, OverflowError):
        parsed = float(CONNECTOR_TIMEOUT_SECONDS)
    if not math.isfinite(parsed):
        parsed = float(CONNECTOR_TIMEOUT_SECONDS)
    return max(CONNECTOR_MIN_TIMEOUT_SECONDS, min(parsed, float(CONNECTOR_TIMEOUT_SECONDS)))


__all__ = [
    "CONNECTOR_RESULT_LIMIT",
    "CONNECTOR_SEARCH_IDS",
    "ScholarlySources",
]
