"""Search scope and provenance shared by acquisition and receipt recovery."""
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, fields
from typing import Protocol, runtime_checkable
from urllib.parse import parse_qs, urlsplit

from codey.types.json_value import JsonObject, JsonValue, decode_json_object, require_json_integer, require_json_string
from codey.utils.refs import normalize_text

_SITE = re.compile(r'(?<!\S)site:([^\s"\'()]+)', re.IGNORECASE)
EXCLUDED_SEARCH_REGIONS = (
    'nav,header,footer,[role="navigation"],[role="banner"],'
    '[data-text-ad],[data-ad],.b_ad,.b_adTop,.b_adBottom,.result--ad,[aria-label="Ads"]'
)


def organic_search_selector(selector: str) -> str:
    return selector + ":not(:is(" + EXCLUDED_SEARCH_REGIONS + "),:is(" + EXCLUDED_SEARCH_REGIONS + ") *)"


@dataclass(frozen=True)
class SearchAttempt:
    requested_query: str
    engine: str
    final_url: str = ""
    extraction_scope: str = "unobserved"
    query_consistent: bool | None = None
    accepted_urls: tuple[str, ...] = ()
    rejected_urls: tuple[str, ...] = ()
    failure_kind: str = ""
    version: int = 2
    state: str = "unknown"
    effective_query: str = ""
    coverage: str = "web"
    detail: str = ""
    diagnostics: tuple[JsonObject, ...] = ()


@runtime_checkable
class SearchProvenanceProvider(Protocol):
    @property
    def search_attempts(self) -> tuple[SearchAttempt, ...]: ...


def provider_search_attempts(provider: object) -> tuple[SearchAttempt, ...]:
    return provider.search_attempts if isinstance(provider, SearchProvenanceProvider) else ()


def normalized_hostname(value: str) -> str:
    try:
        return (urlsplit(value).hostname or "").rstrip(".").encode("idna").decode("ascii").lower()
    except (ValueError, UnicodeError):
        return ""


def site_hosts(query: str) -> tuple[str, ...]:
    hosts: list[str] = []
    for token in _SITE.findall(query):
        host = normalized_hostname(token if "://" in token else "https://" + token)
        if not host:
            raise ValueError("site: requires a valid hostname")
        hosts.append(host)
    return tuple(dict.fromkeys(hosts))


def filter_site_results(query: str, rows: Sequence[JsonObject]) -> tuple[list[JsonObject], tuple[str, ...]]:
    hosts = site_hosts(query)
    accepted: list[JsonObject] = []
    rejected: list[str] = []
    for row in rows:
        url = require_json_string(row["url"], "search result URL")
        host = normalized_hostname(url)
        if hosts and not any(host == scope or host.endswith("." + scope) for scope in hosts):
            rejected.append(url)
        else:
            accepted.append(row)
    return accepted, tuple(rejected)


def query_consistency(final_url: str, query: str) -> bool | None:
    values = parse_qs(urlsplit(final_url).query, keep_blank_values=True).get("q")
    if values is None:
        return None
    return len(values) == 1 and normalize_text(values[0]) == normalize_text(query)


def decode_search_attempt(value: object) -> SearchAttempt:
    row = decode_json_object(value)
    if "version" not in row:
        legacy = {item.name for item in fields(SearchAttempt)} - {
            "version", "state", "effective_query", "coverage", "detail", "diagnostics"}
        if set(row) != legacy:
            raise ValueError("invalid legacy search provenance")
        row = {**row, "version": 2, "state": "unknown", "effective_query": "", "coverage": "web",
               "detail": "", "diagnostics": list[JsonValue]()}
    if set(row) != {item.name for item in fields(SearchAttempt)}:
        raise ValueError("invalid search provenance")
    if require_json_integer(row["version"], "search version") != 2:
        raise ValueError("unsupported search provenance version")
    state = require_json_string(row["state"], "search state")
    if state not in {"unknown", "hits", "empty", "filtered_empty", "failed"}:
        raise ValueError("invalid search state")
    diagnostics = row["diagnostics"]
    if not isinstance(diagnostics, list):
        raise ValueError("invalid search diagnostics")
    consistent = row["query_consistent"]
    if consistent is not None and not isinstance(consistent, bool):
        raise ValueError("invalid query consistency")
    def urls(key: str) -> tuple[str, ...]:
        raw = row[key]
        if not isinstance(raw, list):
            raise ValueError("invalid search provenance URLs")
        return tuple(require_json_string(item, key) for item in raw)
    return SearchAttempt(
        requested_query=require_json_string(row["requested_query"], "requested_query"),
        engine=require_json_string(row["engine"], "engine"),
        final_url=require_json_string(row["final_url"], "final_url"),
        extraction_scope=require_json_string(row["extraction_scope"], "extraction_scope"),
        query_consistent=consistent, accepted_urls=urls("accepted_urls"), rejected_urls=urls("rejected_urls"),
        failure_kind=require_json_string(row["failure_kind"], "failure_kind"), state=state,
        effective_query=require_json_string(row["effective_query"], "effective query"),
        coverage=require_json_string(row["coverage"], "coverage"), detail=require_json_string(row["detail"], "detail"),
        diagnostics=tuple(decode_json_object(item) for item in diagnostics))
