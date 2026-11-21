"""Search scope and provenance shared by acquisition and receipt recovery."""
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, fields
from typing import Protocol, runtime_checkable
from urllib.parse import parse_qs, urlsplit

from codey.types.json_value import JsonObject, decode_json_object, require_json_string

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
    values = parse_qs(urlsplit(final_url).query).get("q")
    return values == [query] if values is not None else None


def decode_search_attempt(value: object) -> SearchAttempt:
    row = decode_json_object(value)
    if set(row) != {item.name for item in fields(SearchAttempt)}:
        raise ValueError("invalid search provenance")
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
        failure_kind=require_json_string(row["failure_kind"], "failure_kind"))
