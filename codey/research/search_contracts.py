"""One immutable search result, including the facts behind empty and failed searches."""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

from codey.research.search_provenance import SearchAttempt, filter_site_results
from codey.research.search_urls import _looks_like_public_result_url, _normalize_result_url
from codey.types.json_value import JsonObject, require_json_string


class SearchState(StrEnum):
    HITS = "hits"
    EMPTY = "empty"
    FILTERED_EMPTY = "filtered_empty"
    FAILED = "failed"


@dataclass(frozen=True)
class SearchHit:
    title: str
    url: str
    snippet: str = ""

    def row(self) -> JsonObject:
        return {"title": self.title, "url": self.url, "snippet": self.snippet}


@dataclass(frozen=True)
class SearchOutcome:
    state: SearchState
    hits: tuple[SearchHit, ...] = ()
    attempts: tuple[SearchAttempt, ...] = ()
    failure_kind: str = ""
    detail: str = ""

    def __post_init__(self) -> None:
        if (self.state == SearchState.HITS) != bool(self.hits):
            raise ValueError("search state differs from its hits")
        if (self.state == SearchState.FAILED) != bool(self.failure_kind):
            raise ValueError("search state differs from its failure")
        if self.state == SearchState.EMPTY and not any(item.state == "empty" for item in self.attempts):
            raise ValueError("empty search requires a completed zero-result observation")
        if self.state == SearchState.FILTERED_EMPTY and not any(item.rejected_urls for item in self.attempts):
            raise ValueError("filtered search requires rejected candidates")

    @property
    def rows(self) -> list[JsonObject]:
        return [hit.row() for hit in self.hits]

    @property
    def errors(self) -> list[dict[str, str]]:
        return [{"engine": item.engine, "failure_kind": item.failure_kind, "error": item.detail}
                for item in self.attempts if item.failure_kind]


def failed_search(query: str, engine: str, kind: str, detail: str = "", *,
                  final_url: str = "", diagnostics: tuple[JsonObject, ...] = ()) -> SearchOutcome:
    return SearchOutcome(SearchState.FAILED, attempts=(SearchAttempt(query, engine, final_url=final_url,
        failure_kind=kind, state="failed", detail=detail, diagnostics=diagnostics),), failure_kind=kind, detail=detail)


def complete_search(query: str, rows: list[JsonObject], attempt: SearchAttempt, limit: int, *,
                    explicit_empty: bool = False) -> SearchOutcome:
    normalized: list[JsonObject] = []
    seen: set[str] = set()
    for row in rows:
        url = _normalize_result_url(require_json_string(row["url"], "search URL"))
        if url in seen or not _looks_like_public_result_url(url):
            continue
        title = require_json_string(row["title"], "search title").strip()
        if not title:
            continue
        seen.add(url)
        normalized.append({"url": url, "title": title, "snippet": require_json_string(row.get("snippet", ""), "snippet")})
    if rows and not normalized:
        return failed_search(query, attempt.engine, "search_invalid_results", final_url=attempt.final_url)
    accepted, rejected = filter_site_results(query, normalized)
    hits = tuple(SearchHit(require_json_string(row["title"], "title"), require_json_string(row["url"], "URL"),
                           require_json_string(row["snippet"], "snippet")) for row in accepted[:limit])
    if hits:
        state = SearchState.HITS
    elif rejected:
        state = SearchState.FILTERED_EMPTY
    elif explicit_empty:
        state = SearchState.EMPTY
    else:
        return failed_search(query, attempt.engine, "search_page_blank", final_url=attempt.final_url)
    return SearchOutcome(state, hits, (replace(attempt, state=state.value,
        accepted_urls=tuple(hit.url for hit in hits), rejected_urls=rejected),))
