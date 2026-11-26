"""Optional SearXNG JSON search at an explicitly configured user endpoint."""
from __future__ import annotations

import os
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from http.client import HTTPException
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request

from codey.env_names import SEARCH_API_TOKEN_ENV, SEARCH_SEARXNG_URL_ENV
from codey.policies.network import check_fetch_url
from codey.research.http_redirects import build_no_redirect_opener
from codey.research.search_contracts import SearchOutcome, complete_search, failed_search
from codey.research.search_provenance import SearchAttempt
from codey.runtime.core import cancellation
from codey.types.json_value import JsonObject, decode_json_object, parse_json_object

MAX_RESPONSE_BYTES = 1024 * 1024


class _SearchResponse(Protocol):
    @property
    def status(self) -> int: ...
    def read(self, size: int) -> bytes: ...


@dataclass(frozen=True)
class SearchApi:
    endpoint: str
    token: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        parsed = urlsplit(self.endpoint)
        if parsed.port == 0:
            raise ValueError("invalid SearXNG port")
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.fragment:
            raise ValueError("invalid SearXNG endpoint")
        if parsed.query:
            raise ValueError("configure a search endpoint without query parameters")
        if self.token and parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("search credentials require HTTPS")

    @classmethod
    def configured(cls) -> SearchApi | None:
        endpoint = os.environ.get(SEARCH_SEARXNG_URL_ENV, "").strip()
        return cls(endpoint, os.environ.get(SEARCH_API_TOKEN_ENV, "")) if endpoint else None

    def search(self, query: str, *, limit: int, timeout: float) -> SearchOutcome:
        cancellation.check()
        parsed = urlsplit(self.endpoint)
        # Only this explicitly configured endpoint may be local. Result URLs keep
        # the ordinary acquisition policy, and redirects are never followed.
        if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            reason = check_fetch_url(self.endpoint, use_cache=True)
            if reason:
                return failed_search(query, "searxng", "search_api_policy_denied", reason)
        url = urlunsplit(parsed._replace(query=urlencode([*parse_qsl(parsed.query), ("q", query), ("format", "json")])))
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        request = Request(url, headers=headers)
        try:
            opened: AbstractContextManager[_SearchResponse] = build_no_redirect_opener().open(request, timeout=timeout)
            with opened as response:
                if response.status != 200:
                    return failed_search(query, "searxng", "search_api_http_error", str(response.status))
                body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise ValueError("search response exceeds byte limit")
            cancellation.check()
            payload = parse_json_object(body.decode("utf-8"))
            rows = payload.get("results")
            if not isinstance(rows, list):
                raise ValueError("SearXNG results must be an array")
            if not rows and (payload.get("unresponsive_engines") or payload.get("errors")):
                return failed_search(query, "searxng", "search_api_upstream_failed")
            candidates: list[JsonObject] = []
            for item in rows:
                row = decode_json_object(item)
                candidates.append({"url": row.get("url"), "title": row.get("title"), "snippet": row.get("content", "")})
            return complete_search(query, candidates,
                SearchAttempt(query, "searxng", effective_query=query, extraction_scope="api_results"),
                limit, explicit_empty=not rows)
        except HTTPError as exc:
            status = exc.code
            exc.close()
            cancellation.check()
            return failed_search(query, "searxng", "search_api_http_error", str(status))
        except (HTTPException, URLError, OSError, ValueError) as exc:
            cancellation.check()
            return failed_search(query, "searxng", "search_api_failed", type(exc).__name__)
