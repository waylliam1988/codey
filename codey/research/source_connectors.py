"""Source connector contracts and recorded fixture readers.

The connector layer is a local Research boundary. It describes source types and
turns recorded/local inputs into stable source hits or fetched sources. It does
not call models, open browsers, dispatch runtime tools, or write state.
"""

from __future__ import annotations

import csv
import io
import json
import mimetypes
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping
from urllib.parse import quote, urlparse

from codey.research.connector_domains import (
    ARXIV_CONNECTOR_TERMS,
    LOCAL_CONNECTOR_TERMS,
    MEDICAL_CONNECTOR_TERMS,
)
from codey.utils.refs import (
    bounded_refs,
    clip,
    digest_text,
    identifier,
    stable_ref,
)
from codey.research.identity import (
    path_ref,
    sanitize_research_url_ref,
)
from codey.policies.redaction import (
    SECRET_MARKER_RE,
    SECRET_SHAPE_RE,
    looks_prompt_visible_secret,
    looks_sensitive_code,
)
from codey.research.shape import (
    bounded_limit as _bounded_limit,
    connector_id as _connector_id,
    valid_digest_ref,
    generated_ref as _generated_ref,
)
from codey.research.source_document import SourceDocument
from codey.policies.network import check_fetch_url


CONNECTOR_STATUS_AVAILABLE = "available"
CONNECTOR_STATUS_FIXTURE = "fixture"
CONNECTOR_STATUS_EXPERIMENTAL = "experimental"
CONNECTOR_STATUS_OPTIONAL = "optional"
CONNECTOR_STATUS_UNAVAILABLE = "unavailable"
CONNECTOR_AVAILABLE_STATUSES = frozenset({
    CONNECTOR_STATUS_AVAILABLE,
    CONNECTOR_STATUS_FIXTURE,
    CONNECTOR_STATUS_EXPERIMENTAL,
})
MAX_CONNECTOR_HITS = 12
MAX_FETCH_BYTES = 512 * 1024
MAX_FETCH_CHARS = 120_000
MAX_TABLE_ROWS = 24
MAX_JSON_CHARS = 80_000
_SPACE_RE = re.compile(r"\s+")
_SAFE_QUERY_TOKEN_RE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9_+-]*(?:/[A-Za-z0-9][A-Za-z0-9_+-]*)+|"
    r"[A-Za-z][A-Za-z0-9_+-]{2,}|20\d{2}|\d+(?:\.\d+)?|[\u4e00-\u9fff]{2,}"
)
_SAFE_SLASH_TERM_RE = re.compile(
    r"(?=.{3,80}$)[A-Za-z0-9][A-Za-z0-9_+-]*"
    r"(?:/[A-Za-z0-9][A-Za-z0-9_+-]*)+"
)
_SAFE_SCIENTIFIC_SLASH_PART_RE = re.compile(
    r"(?:[A-Z]{2,}[A-Za-z0-9+-]*|[a-z][A-Z]{2,}[A-Za-z0-9+-]*|p\d+[A-Za-z0-9+-]*)"
)
_SECRET_VALUE_CONNECTORS = frozenset({
    "as",
    "called",
    "configured",
    "equal",
    "equals",
    "is",
    "known",
    "named",
    "set",
    "to",
    "value",
    "was",
    "为",
    "叫",
    "叫做",
    "名为",
    "是",
    "等于",
    "设置为",
})
_SECRET_VALUE_SEPARATORS = frozenset(":=,;-–—−/\\|，。；：、._+·•")
_SECRET_ASSIGNMENT_SEPARATORS = frozenset(":=：＝")
_SECRET_VALUE_CONNECTOR_LIMIT = 12
_SECRET_QUERY_TOKEN_RE = re.compile(r"[A-Za-z0-9]+|[\u4e00-\u9fff]+")
_SECRET_VALUE_TOKEN_RE = re.compile(
    r"(?i)(?:"
    r"[a-f0-9]{6,}|"
    r"(?=[a-z0-9_-]{6,}$)(?=.*[a-z])(?=.*\d)[a-z0-9_-]+|"
    r"[a-z0-9_-]{20,}"
    r")"
)
_SECRET_QUERY_MARKER_PHRASES = frozenset({
    ("access", "key"),
    ("access", "token"),
    ("api", "key"),
    ("api", "token"),
    ("auth", "token"),
    ("bearer", "token"),
    ("client", "secret"),
    ("id", "token"),
    ("private", "key"),
    ("refresh", "token"),
    ("session", "id"),
    ("ssh", "key"),
    ("密", "钥"),
    ("密", "码"),
    ("令", "牌"),
    ("私", "钥"),
    ("访问", "令牌"),
})
_SECRET_QUERY_MARKER_COMPOUNDS = frozenset({
    "accesskey",
    "accesstoken",
    "apikey",
    "apitoken",
    "authtoken",
    "bearertoken",
    "clientsecret",
    "idtoken",
    "privatekey",
    "refreshtoken",
    "sessionid",
    "sshkey",
})
_PLAIN_VALUE_SECRET_MARKERS = frozenset({
    "passwd",
    "password",
    "passphrase",
    "pwd",
    "令牌",
    "密码",
    "密钥",
    "私钥",
    "访问令牌",
})
_VALUE_SCHEME_SECRET_MARKERS = frozenset({"bearer"})
_CONTEXTUAL_VALUE_SECRET_MARKERS = frozenset({"cookie", "jwt", "token"})
_MULTI_TOKEN_VALUE_SECRET_MARKERS = frozenset({
    *_SECRET_QUERY_MARKER_COMPOUNDS,
    *_PLAIN_VALUE_SECRET_MARKERS,
    *_VALUE_SCHEME_SECRET_MARKERS,
    "authorization",
})
_MULTI_TOKEN_SECRET_VALUE_LIMIT = 6
_SECRET_VALUE_BOUNDARY_TERMS = frozenset({
    *ARXIV_CONNECTOR_TERMS,
    *LOCAL_CONNECTOR_TERMS,
    *MEDICAL_CONNECTOR_TERMS,
    "budget",
    "consent",
    "cryptography",
    "efficient",
    "efficiency",
    "exceeded",
    "policy",
    "required",
    "sharing",
    "transformers",
})
_ALLOWED_HIT_CONTENT_KINDS = frozenset({"abstract", "html", "json", "pdf", "table", "text"})
_ALLOWED_FETCHED_CONTENT_KINDS = _ALLOWED_HIT_CONTENT_KINDS
_ALLOWED_FETCHED_MIME_TYPES = frozenset({
    "application/json",
    "application/pdf",
    "application/xml",
    "text/csv",
    "text/html",
    "text/plain",
    "text/tab-separated-values",
    "text/xml",
})
_ALLOWED_HIT_SOURCE_KINDS = frozenset({
    "biomedical_literature",
    "dataset",
    "local_file",
    "preprint",
    "table",
})
_SAFE_PUBLISHED_AT_RE = re.compile(
    r"^\d{4}(?:-\d{2}(?:-\d{2})?)?"
    r"(?:[T ][0-2]\d:[0-5]\d(?::[0-5]\d)?(?:Z|[+-][0-2]\d:?[0-5]\d)?)?$"
)
_ARXIV_ID_RE = re.compile(
    r"^(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[a-z]{2})?/\d{7})(?:v\d+)?$",
    re.IGNORECASE,
)
_SAFE_QUERY_STOP_TERMS = frozenset({
    "about",
    "answer",
    "are",
    "based",
    "can",
    "does",
    "find",
    "for",
    "from",
    "how",
    "investigate",
    "latest",
    "need",
    "please",
    "question",
    "research",
    "should",
    "study",
    "the",
    "this",
    "what",
    "whether",
    "when",
    "where",
    "which",
    "why",
    "with",
    "研究",
    "调查",
    "查找",
    "问题",
    "什么",
    "如何",
    "是否",
})
_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}
_CONNECTOR_ALLOWED_HOSTS = {
    "arxiv": frozenset({"arxiv.org"}),
    "pubmed": frozenset({"pubmed.ncbi.nlm.nih.gov"}),
}


@dataclass(frozen=True)
class SourceConnectorSpec:
    id: str
    kind: str
    status: str = CONNECTOR_STATUS_UNAVAILABLE
    search_supported: bool = False
    fetch_supported: bool = False
    fixture_supported: bool = False
    shipped: bool = False
    local: bool = False
    rate_limit_seconds: float = 0.0
    source_quality_hint: Mapping[str, object] = field(default_factory=dict)
    failure_modes: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": _safe_connector_code(self.id),
            "kind": _safe_connector_code(self.kind),
            "status": _connector_status(self.status),
            "search_supported": bool(self.search_supported),
            "fetch_supported": bool(self.fetch_supported),
            "fixture_supported": bool(self.fixture_supported),
            "shipped": bool(self.shipped),
            "local": bool(self.local),
            "rate_limit_seconds": max(0.0, round(float(self.rate_limit_seconds or 0.0), 3)),
            "source_quality_hint": _bounded_mapping(self.source_quality_hint),
            "failure_modes": list(_safe_payload_refs(self.failure_modes, limit=8)),
        }
        return payload


@dataclass(frozen=True)
class SafeConnectorQuery:
    terms: tuple[str, ...] = ()
    redacted: bool = False
    skip_reason: str = ""


@dataclass(frozen=True)
class SourceHit:
    connector_id: str
    hit_id: str
    source_ref: str
    source_id: str
    title: str = ""
    snippet: str = ""
    content_kind: str = "text"
    source_kind: str = ""
    canonical_url: str = ""
    score: float = 0.0
    published_at: str = ""
    metadata_refs: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "connector_id": _connector_id(self.connector_id),
            "hit_id": _generated_ref(self.hit_id, "source_hit"),
            "source_ref": _generated_ref(self.source_ref, "source_ref"),
            "source_id": _generated_ref(self.source_id, "connector_source"),
            "content_kind": _safe_hit_content_kind(self.content_kind),
            "source_kind": _safe_hit_source_kind(self.source_kind),
            "score": _score(self.score),
            "title_digest": digest_text(clip(self.title, 180)),
            "snippet_digest": digest_text(clip(self.snippet, 360)),
            "title_chars": len(str(self.title or "")),
            "snippet_chars": len(str(self.snippet or "")),
            "evidence_ready": False,
        }
        if self.canonical_url:
            payload["url_ref"] = sanitize_research_url_ref(self.canonical_url)
        if self.published_at:
            published_at = _safe_published_at(self.published_at)
            if published_at:
                payload["published_at"] = published_at
        refs = bounded_refs(
            (item for item in self.metadata_refs if not looks_prompt_visible_secret(item)),
            limit=8,
        )
        if refs:
            payload["metadata_refs"] = list(refs)
        return payload


@dataclass(frozen=True)
class FetchedSource:
    connector_id: str
    source_ref: str
    source_id: str
    document: SourceDocument
    content_digest: str
    warnings: tuple[str, ...] = ()

    @classmethod
    def from_document(
        cls,
        *,
        connector_id: str,
        source_ref: str,
        source_id: str,
        document: SourceDocument,
        warnings: Iterable[object] = (),
    ) -> "FetchedSource":
        return cls(
            connector_id=_connector_id(connector_id),
            source_ref=_generated_ref(source_ref, "source_ref"),
            source_id=_generated_ref(source_id, "connector_source"),
            document=document,
            content_digest=digest_text(document.text),
            warnings=_safe_payload_refs(warnings, limit=8),
        )

    def to_source_document(self) -> SourceDocument:
        return self.document

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "connector_id": _connector_id(self.connector_id),
            "source_ref": _generated_ref(self.source_ref, "source_ref"),
            "source_id": _generated_ref(self.source_id, "connector_source"),
            "content_digest": valid_digest_ref(self.content_digest),
            "content_kind": _safe_fetched_content_kind(self.document.content_kind),
            "mime_type": _safe_mime_type(self.document.mime_type),
            "text_chars": len(str(self.document.text or "")),
            "title_digest": digest_text(clip(self.document.title, 180)),
            "truncated": bool(self.document.truncated),
            "url_ref": sanitize_research_url_ref(self.document.final_url),
            "fetched": True,
            "evidence_ready": False,
        }
        if self.document.page_count:
            payload["page_count"] = max(0, int(self.document.page_count))
        if self.warnings:
            payload["warnings"] = list(_safe_payload_refs(self.warnings, limit=8))
        return payload


@dataclass(frozen=True)
class SourceConnectorResult:
    connector_id: str
    query_digest: str = ""
    hits: tuple[SourceHit, ...] = ()
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, object]:
        return {
            "connector_id": _connector_id(self.connector_id),
            "query_digest": valid_digest_ref(self.query_digest),
            "hit_count": len(self.hits),
            "hits": [item.to_payload() for item in self.hits[:MAX_CONNECTOR_HITS]],
            "warnings": list(_safe_payload_refs(self.warnings, limit=8)),
            "errors": list(_safe_payload_refs(self.errors, limit=8)),
        }


@dataclass(frozen=True)
class SourceConnectorRegistry:
    specs: tuple[SourceConnectorSpec, ...]

    def __init__(self, specs: Iterable[SourceConnectorSpec]) -> None:
        ordered = tuple(sorted(specs, key=lambda item: item.id))
        object.__setattr__(self, "specs", ordered)
        self.validate()

    def ids(self) -> tuple[str, ...]:
        return tuple(item.id for item in self.specs)

    def all(self) -> tuple[SourceConnectorSpec, ...]:
        return self.specs

    def get(self, connector_id: str) -> SourceConnectorSpec:
        normalized = _connector_id(connector_id)
        for spec in self.specs:
            if spec.id == normalized:
                return spec
        raise KeyError(f"unknown source connector: {connector_id}")

    def available(self) -> tuple[SourceConnectorSpec, ...]:
        return tuple(item for item in self.specs if item.status in CONNECTOR_AVAILABLE_STATUSES)

    def shipped_fixture_ids(self) -> tuple[str, ...]:
        return tuple(item.id for item in self.specs if item.shipped and item.fixture_supported)

    def to_payload(self) -> list[dict[str, object]]:
        return [item.to_payload() for item in self.specs]

    def validate(self) -> None:
        ids = self.ids()
        if len(set(ids)) != len(ids):
            raise ValueError("source connector ids must be unique")
        for spec in self.specs:
            _connector_code_or_raise(spec.id, "connector id")
            _connector_code_or_raise(spec.kind, f"{spec.id}.kind")
            _connector_status(spec.status)
            if spec.shipped and spec.status not in CONNECTOR_AVAILABLE_STATUSES:
                raise ValueError(f"shipped connector {spec.id} must be available")
            for item in spec.failure_modes:
                _connector_code_or_raise(item, f"{spec.id}.failure_modes")
            for key in spec.source_quality_hint:
                _connector_code_or_raise(key, f"{spec.id}.source_quality_hint")


def built_in_connector_registry() -> SourceConnectorRegistry:
    return SourceConnectorRegistry((
        SourceConnectorSpec(
            id="arxiv",
            kind="academic_preprint",
            status=CONNECTOR_STATUS_AVAILABLE,
            search_supported=True,
            fetch_supported=True,
            fixture_supported=True,
            shipped=True,
            rate_limit_seconds=3.0,
            source_quality_hint={"level": "preprint", "kind": "paper", "freshness": "dated"},
            failure_modes=("rate_limited", "fixture_parse_failed", "url_denied"),
        ),
        SourceConnectorSpec(
            id="csv_tsv",
            kind="table_file",
            status=CONNECTOR_STATUS_FIXTURE,
            fetch_supported=True,
            fixture_supported=True,
            shipped=True,
            local=True,
            source_quality_hint={"level": "primary", "kind": "data", "freshness": "undated"},
            failure_modes=("workspace_escape", "file_too_large", "parse_failed"),
        ),
        SourceConnectorSpec(
            id="json_file",
            kind="structured_file",
            status=CONNECTOR_STATUS_FIXTURE,
            fetch_supported=True,
            fixture_supported=True,
            shipped=True,
            local=True,
            source_quality_hint={"level": "primary", "kind": "data", "freshness": "undated"},
            failure_modes=("workspace_escape", "file_too_large", "parse_failed"),
        ),
        SourceConnectorSpec(
            id="local_file",
            kind="local_file",
            status=CONNECTOR_STATUS_FIXTURE,
            fetch_supported=True,
            fixture_supported=True,
            shipped=True,
            local=True,
            source_quality_hint={"level": "primary", "kind": "local_file", "freshness": "undated"},
            failure_modes=("workspace_escape", "file_too_large", "decode_failed"),
        ),
        SourceConnectorSpec(
            id="openalex",
            kind="citation_metadata",
            status=CONNECTOR_STATUS_UNAVAILABLE,
            search_supported=True,
            fetch_supported=False,
            fixture_supported=False,
            shipped=False,
            source_quality_hint={"level": "metadata", "kind": "citation_graph", "freshness": "dated"},
            failure_modes=("deferred_connector_pack",),
        ),
        SourceConnectorSpec(
            id="pubmed",
            kind="biomedical_literature",
            status=CONNECTOR_STATUS_AVAILABLE,
            search_supported=True,
            fetch_supported=True,
            fixture_supported=True,
            shipped=True,
            rate_limit_seconds=0.34,
            source_quality_hint={"level": "primary", "kind": "medical_literature", "freshness": "dated"},
            failure_modes=("rate_limited", "fixture_parse_failed", "url_denied"),
        ),
        SourceConnectorSpec(
            id="rss",
            kind="feed",
            status=CONNECTOR_STATUS_OPTIONAL,
            search_supported=True,
            fetch_supported=True,
            fixture_supported=False,
            shipped=False,
            source_quality_hint={"level": "secondary", "kind": "feed", "freshness": "dated"},
            failure_modes=("optional_connector_pack",),
        ),
    ))


def source_result_from_hits(
    connector_id: str,
    *,
    query: str = "",
    hits: Iterable[SourceHit] = (),
    warnings: Iterable[object] = (),
    errors: Iterable[object] = (),
) -> SourceConnectorResult:
    return SourceConnectorResult(
        connector_id=_connector_id(connector_id),
        query_digest=_safe_query_digest(query),
        hits=tuple(hits)[:MAX_CONNECTOR_HITS],
        warnings=_safe_payload_refs(warnings, limit=8),
        errors=_safe_payload_refs(errors, limit=8),
    )


def safe_connector_query_terms(text: object, *, limit: int = 16) -> tuple[str, ...]:
    """Return bounded non-secret terms that are safe to send to source APIs."""

    return safe_connector_query(text, limit=limit).terms


def safe_connector_query(text: object, *, limit: int = 16) -> SafeConnectorQuery:
    """Return the only terms allowed to leave the connector query boundary."""

    terms: list[str] = []
    seen: set[str] = set()
    cleaned, redacted = _drop_unsafe_query_spans(text)
    for raw in _SAFE_QUERY_TOKEN_RE.findall(cleaned):
        term = safe_connector_signal_text(raw)
        if not term:
            continue
        folded = term.casefold()
        if folded in _SAFE_QUERY_STOP_TERMS or folded in seen:
            continue
        seen.add(folded)
        terms.append(term)
        if len(terms) >= _bounded_limit(limit, default=1, upper=MAX_CONNECTOR_HITS):
            break
    if not terms:
        reason = "connector_query_empty_after_redaction" if redacted else "connector_query_empty"
        return SafeConnectorQuery(terms=(), redacted=redacted, skip_reason=reason)
    return SafeConnectorQuery(terms=tuple(terms), redacted=redacted)


def _safe_hit_content_kind(value: object) -> str:
    text = identifier(value, 40)
    return text if text in _ALLOWED_HIT_CONTENT_KINDS else "text"


def _safe_fetched_content_kind(value: object) -> str:
    text = identifier(value, 40)
    return text if text in _ALLOWED_FETCHED_CONTENT_KINDS else "text"


def _safe_mime_type(value: object) -> str:
    text = str(value or "").strip().lower()
    if not text or looks_prompt_visible_secret(text):
        return ""
    return text if text in _ALLOWED_FETCHED_MIME_TYPES else ""


def _safe_hit_source_kind(value: object) -> str:
    text = identifier(value, 80)
    if not text or looks_prompt_visible_secret(text):
        return ""
    return text if text in _ALLOWED_HIT_SOURCE_KINDS else ""


def _safe_published_at(value: object) -> str:
    text = str(value or "").strip()
    if not text or looks_prompt_visible_secret(text):
        return ""
    return text if _SAFE_PUBLISHED_AT_RE.fullmatch(text) else ""


def safe_connector_signal_text(value: object, *, limit: int = 180) -> str:
    text = clip(value, limit)
    if not text:
        return ""
    if "://" in text or "\\" in text:
        return ""
    if "/" in text and not _safe_scientific_slash_term(text):
        return ""
    if looks_prompt_visible_secret(text):
        return ""
    if re.fullmatch(r"[A-Za-z0-9_./+=:-]{24,}", text):
        return ""
    return text


def connector_query_has_secret_signal(value: object) -> bool:
    """Return whether a raw connector query has a high-confidence secret window."""

    return bool(_secret_query_spans(str(value or "")))


@dataclass(frozen=True)
class _SecretMarkerPolicy:
    allow_plain_value: bool
    contextual: bool = False
    value_token_limit: int = 1


def _secret_query_spans(text: str) -> tuple[tuple[int, int], ...]:
    if not text:
        return ()
    spans: list[tuple[int, int]] = []
    for match in SECRET_SHAPE_RE.finditer(text):
        spans.append(_query_token_bounds(text, match.start(), match.end()))
    for match in SECRET_MARKER_RE.finditer(text):
        marker_tokens = tuple(token for token, _start, _end in _secret_query_tokens(match.group(0)))
        compact = "".join(marker_tokens)
        policy = _secret_marker_policy(marker_tokens, compact)
        span_end = _secret_value_window_end(
            text,
            match.end(),
            policy=policy,
        )
        if span_end > match.end() or (policy.allow_plain_value and not policy.contextual):
            spans.append((
                _span_start_for_marker(text, match.start()),
                max(span_end, match.end()),
            ))
    tokens = _secret_query_tokens(text)
    for index, (token, _start, end) in enumerate(tokens):
        if token in _VALUE_SCHEME_SECRET_MARKERS:
            span_end = _secret_value_window_end(
                text,
                end,
                policy=_secret_marker_policy((token,), token),
            )
            if span_end > end:
                spans.append((_span_start_for_marker(text, tokens[index][1]), span_end))
        if token in _CONTEXTUAL_VALUE_SECRET_MARKERS:
            span_end = _secret_value_window_end(
                text,
                end,
                policy=_secret_marker_policy((token,), token),
            )
            if span_end > end:
                spans.append((_span_start_for_marker(text, tokens[index][1]), span_end))
        if index + 1 < len(tokens):
            pair = (token, tokens[index + 1][0])
            if pair in _SECRET_QUERY_MARKER_PHRASES:
                compact = "".join(pair)
                policy = _secret_marker_policy(pair, compact)
                span_end = _secret_value_window_end(
                    text,
                    tokens[index + 1][2],
                    policy=policy,
                )
                if span_end > tokens[index + 1][2] or not policy.contextual:
                    spans.append((
                        _span_start_for_marker(text, tokens[index][1]),
                        max(span_end, tokens[index + 1][2]),
                    ))
    return tuple(spans)


def is_valid_pubmed_id(value: object) -> bool:
    text = str(value or "").strip()
    return bool(text) and text.isdigit() and len(text) <= 12


def is_valid_arxiv_id(value: object) -> bool:
    return bool(_ARXIV_ID_RE.fullmatch(str(value or "").strip()))


def _safe_query_digest(query: object) -> str:
    safe_query = " ".join(safe_connector_query_terms(query))
    return digest_text(safe_query) if safe_query else ""


def _secret_query_tokens(text: str) -> tuple[tuple[str, int, int], ...]:
    return tuple(
        (match.group(0).casefold(), match.start(), match.end())
        for match in _SECRET_QUERY_TOKEN_RE.finditer(text)
    )


def _secret_marker_policy(marker_tokens: tuple[str, ...], compact: str) -> _SecretMarkerPolicy:
    contextual = len(marker_tokens) == 1 and compact in _CONTEXTUAL_VALUE_SECRET_MARKERS
    explicit = (
        marker_tokens in _SECRET_QUERY_MARKER_PHRASES
        or compact in _SECRET_QUERY_MARKER_COMPOUNDS
        or compact in _PLAIN_VALUE_SECRET_MARKERS
        or compact in _VALUE_SCHEME_SECRET_MARKERS
    )
    return _SecretMarkerPolicy(
        allow_plain_value=explicit or contextual,
        contextual=contextual,
        value_token_limit=(
            _MULTI_TOKEN_SECRET_VALUE_LIMIT
            if compact in _MULTI_TOKEN_VALUE_SECRET_MARKERS
            else 1
        ),
    )


def _secret_value_window_end(
    text: str,
    index: int,
    *,
    policy: _SecretMarkerPolicy,
) -> int:
    cursor = index
    saw_connector = False
    connector_count = 0
    while True:
        token, start, end, had_separator = _query_token_after(text, cursor)
        if not token:
            return index
        if token in _SECRET_VALUE_CONNECTORS:
            saw_connector = True
            connector_count += 1
            if connector_count >= _SECRET_VALUE_CONNECTOR_LIMIT:
                return len(text)
            cursor = end
            continue
        if not (policy.allow_plain_value or saw_connector or had_separator):
            return index
        assignment_context = _has_assignment_separator(text[cursor:start])
        if (
            policy.contextual
            and not (saw_connector or assignment_context)
            and not _looks_secret_value_token(token)
        ):
            return index
        if not (saw_connector or had_separator) and _is_secret_value_boundary(token):
            return index
        return _secret_value_phrase_end(text, end, value_token_limit=policy.value_token_limit)


def _secret_value_phrase_end(text: str, end: int, *, value_token_limit: int) -> int:
    cursor = end
    value_end = end
    value_count = 1
    limit = _bounded_limit(value_token_limit, default=1, upper=_MULTI_TOKEN_SECRET_VALUE_LIMIT)
    while value_count < limit:
        token, _start, next_end, _had_separator = _query_token_after(text, cursor)
        if not token or token in _SECRET_VALUE_CONNECTORS or _is_secret_value_boundary(token):
            break
        value_end = next_end
        cursor = next_end
        value_count += 1
    return value_end


def _looks_secret_value_token(token: str) -> bool:
    return bool(token) and not _is_secret_value_boundary(token) and bool(_SECRET_VALUE_TOKEN_RE.fullmatch(token))


def _has_assignment_separator(text: str) -> bool:
    return any(char in _SECRET_ASSIGNMENT_SEPARATORS for char in text)


def _is_secret_value_boundary(token: str) -> bool:
    if not token:
        return True
    folded = token.casefold()
    if folded in _SAFE_QUERY_STOP_TERMS or folded in _SECRET_VALUE_BOUNDARY_TERMS:
        return True
    return False


def _query_token_after(text: str, index: int) -> tuple[str, int, int, bool]:
    had_separator = False
    while index < len(text) and (text[index].isspace() or text[index] in _SECRET_VALUE_SEPARATORS):
        had_separator = had_separator or text[index] in _SECRET_VALUE_SEPARATORS
        index += 1
    match = _SECRET_QUERY_TOKEN_RE.match(text, index)
    if not match:
        return "", index, index, had_separator
    return match.group(0).casefold(), match.start(), match.end(), had_separator


def _span_start_for_marker(text: str, start: int) -> int:
    while start > 0 and (text[start - 1].isspace() or text[start - 1] in _SECRET_VALUE_SEPARATORS):
        start -= 1
    return start


def _drop_unsafe_query_spans(value: object) -> tuple[str, bool]:
    parts: list[str] = []
    cleaned, redacted = _mask_secret_query_spans(str(value or "").replace("\r", " ").replace("\n", " "))
    for raw in cleaned.split():
        token = raw.strip()
        if not token:
            continue
        if _unsafe_query_span(token):
            parts.append(" ")
            redacted = True
            continue
        parts.append(token)
    return " ".join(parts), redacted


def _mask_secret_query_spans(text: str) -> tuple[str, bool]:
    if not text:
        return "", False
    spans = list(_secret_query_spans(text))
    if not spans:
        return text, False
    chars = list(text)
    for start, end in spans:
        for index in range(max(0, start), min(len(chars), end)):
            chars[index] = " "
    return "".join(chars), True


def _query_token_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    while start > 0 and not text[start - 1].isspace():
        start -= 1
    while end < len(text) and not text[end].isspace():
        end += 1
    return start, end


def _unsafe_query_span(token: str) -> bool:
    text = token.strip(".,;()[]{}<>\"'")
    if not text:
        return True
    if "://" in text or "\\" in text:
        return True
    if re.match(r"^[A-Za-z]:/", text):
        return True
    if text.startswith(("/", "./", "../", "~/")):
        return True
    if "/" in text and not _safe_scientific_slash_term(text):
        return True
    return False


def _safe_scientific_slash_term(text: str) -> bool:
    if not _SAFE_SLASH_TERM_RE.fullmatch(text):
        return False
    parts = text.split("/")
    if len(parts) > 4:
        return False
    return all(_SAFE_SCIENTIFIC_SLASH_PART_RE.fullmatch(part) for part in parts)


def fetch_local_file(
    path: str | Path,
    *,
    allowed_roots: Iterable[str | Path],
    connector_id: str = "local_file",
    max_bytes: int = MAX_FETCH_BYTES,
) -> FetchedSource:
    resolved, root = resolve_local_source_path(path, allowed_roots=allowed_roots)
    data = _read_limited_bytes(resolved, max_bytes=max_bytes)
    text = data.decode("utf-8", errors="replace")
    truncated = len(text) > MAX_FETCH_CHARS
    if truncated:
        text = text[:MAX_FETCH_CHARS]
    source_ref = _source_ref_for_path(connector_id, resolved, root)
    source_id = stable_ref("connector_source", connector_id, source_ref)
    document = SourceDocument(
        requested_url=_connector_locator(connector_id, source_ref, resolved.name),
        final_url=_connector_locator(connector_id, source_ref, resolved.name),
        title=resolved.name,
        content_kind="text",
        mime_type=mimetypes.guess_type(resolved.name)[0] or "text/plain",
        text=text,
        truncated=truncated,
    )
    return FetchedSource.from_document(
        connector_id=connector_id,
        source_ref=source_ref,
        source_id=source_id,
        document=document,
        warnings=("text_truncated",) if truncated else (),
    )


def fetch_csv_tsv_file(
    path: str | Path,
    *,
    allowed_roots: Iterable[str | Path],
    max_bytes: int = MAX_FETCH_BYTES,
    max_rows: int = MAX_TABLE_ROWS,
) -> FetchedSource:
    resolved, root = resolve_local_source_path(path, allowed_roots=allowed_roots)
    data = _read_limited_bytes(resolved, max_bytes=max_bytes)
    text = data.decode("utf-8-sig", errors="replace")
    delimiter = "\t" if resolved.suffix.lower() == ".tsv" else ","
    row_limit = _positive_int(max_rows, MAX_TABLE_ROWS)
    rows = _read_csv_rows(text, delimiter=delimiter, max_rows=row_limit + 1)
    truncated = len(rows) > row_limit
    rendered = _render_table_rows(rows[:row_limit], delimiter=delimiter)
    source_ref = _source_ref_for_path("csv_tsv", resolved, root)
    source_id = stable_ref("connector_source", "csv_tsv", source_ref)
    document = SourceDocument(
        requested_url=_connector_locator("csv_tsv", source_ref, resolved.name),
        final_url=_connector_locator("csv_tsv", source_ref, resolved.name),
        title=resolved.name,
        content_kind="table",
        mime_type="text/tab-separated-values" if delimiter == "\t" else "text/csv",
        text=rendered,
        truncated=truncated,
    )
    return FetchedSource.from_document(
        connector_id="csv_tsv",
        source_ref=source_ref,
        source_id=source_id,
        document=document,
        warnings=("rows_truncated",) if truncated else (),
    )


def fetch_json_file(
    path: str | Path,
    *,
    allowed_roots: Iterable[str | Path],
    max_bytes: int = MAX_FETCH_BYTES,
) -> FetchedSource:
    resolved, root = resolve_local_source_path(path, allowed_roots=allowed_roots)
    data = _read_limited_bytes(resolved, max_bytes=max_bytes)
    text = data.decode("utf-8-sig", errors="replace")
    parsed = json.loads(text)
    rendered = json.dumps(parsed, ensure_ascii=False, sort_keys=True, indent=2)
    truncated = len(rendered) > MAX_JSON_CHARS
    if truncated:
        rendered = rendered[:MAX_JSON_CHARS]
    source_ref = _source_ref_for_path("json_file", resolved, root)
    source_id = stable_ref("connector_source", "json_file", source_ref)
    document = SourceDocument(
        requested_url=_connector_locator("json_file", source_ref, resolved.name),
        final_url=_connector_locator("json_file", source_ref, resolved.name),
        title=resolved.name,
        content_kind="json",
        mime_type="application/json",
        text=rendered,
        truncated=truncated,
    )
    return FetchedSource.from_document(
        connector_id="json_file",
        source_ref=source_ref,
        source_id=source_id,
        document=document,
        warnings=("json_truncated",) if truncated else (),
    )


def resolve_local_source_path(
    path: str | Path,
    *,
    allowed_roots: Iterable[str | Path],
) -> tuple[Path, Path]:
    roots = tuple(_resolved_root(item) for item in allowed_roots if str(item or "").strip())
    if not roots:
        raise ValueError("allowed_roots required")
    raw = Path(path).expanduser()
    last_error: Exception | None = None
    for root in roots:
        try:
            candidate = raw.resolve() if raw.is_absolute() else (root / raw).resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            last_error = exc
            continue
        if candidate == root or root in candidate.parents:
            if not candidate.is_file():
                raise ValueError("source path is not a file")
            return candidate, root
    if last_error is not None:
        raise ValueError("source path resolution failed") from last_error
    raise ValueError("source path escapes allowed roots")


def parse_arxiv_atom_fixture(xml_text: str, *, query: str = "", limit: int = MAX_CONNECTOR_HITS) -> SourceConnectorResult:
    hits: list[SourceHit] = []
    hit_limit = _bounded_limit(limit, default=MAX_CONNECTOR_HITS, upper=MAX_CONNECTOR_HITS)
    try:
        root = ET.fromstring(str(xml_text or ""))
    except ET.ParseError:
        return source_result_from_hits("arxiv", query=query, errors=("fixture_parse_failed",))
    for entry in root.findall("atom:entry", _ATOM_NS):
        if len(hits) >= hit_limit:
            break
        title = _xml_text(entry, "atom:title")
        summary = _xml_text(entry, "atom:summary")
        canonical = _connector_canonical_url(
            "arxiv",
            _xml_text(entry, "atom:id") or _entry_link(entry, rel="alternate"),
        )
        reason = check_fetch_url(canonical, resolve=False) if canonical else "URL has no host"
        reason = reason or _connector_url_denial_reason("arxiv", canonical)
        if reason:
            continue
        arxiv_id = _arxiv_id(canonical)
        if not is_valid_arxiv_id(arxiv_id):
            continue
        source_ref = _source_ref_for_url("arxiv", canonical)
        source_id = stable_ref("connector_source", "arxiv", source_ref)
        hit_id = stable_ref("source_hit", "arxiv", source_ref, title, arxiv_id)
        hits.append(SourceHit(
            connector_id="arxiv",
            hit_id=hit_id,
            source_ref=source_ref,
            source_id=source_id,
            title=title,
            snippet=summary,
            content_kind="abstract",
            source_kind="preprint",
            canonical_url=canonical,
            published_at=_xml_text(entry, "atom:published"),
            metadata_refs=(f"arxiv:{arxiv_id}",) if arxiv_id else (),
            score=max(0.0, 1.0 - (len(hits) * 0.05)),
        ))
    return source_result_from_hits("arxiv", query=query, hits=hits)


def parse_pubmed_fixture(xml_text: str, *, query: str = "", limit: int = MAX_CONNECTOR_HITS) -> SourceConnectorResult:
    hits: list[SourceHit] = []
    hit_limit = _bounded_limit(limit, default=MAX_CONNECTOR_HITS, upper=MAX_CONNECTOR_HITS)
    try:
        root = ET.fromstring(str(xml_text or ""))
    except ET.ParseError:
        return source_result_from_hits("pubmed", query=query, errors=("fixture_parse_failed",))
    for article in root.findall(".//PubmedArticle"):
        if len(hits) >= hit_limit:
            break
        pmid = _first_text(article, ".//PMID")
        if not is_valid_pubmed_id(pmid):
            continue
        canonical = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
        reason = check_fetch_url(canonical, resolve=False)
        reason = reason or _connector_url_denial_reason("pubmed", canonical)
        if reason:
            continue
        title = _first_text(article, ".//ArticleTitle")
        abstract = _abstract_text(article)
        source_ref = _source_ref_for_url("pubmed", canonical)
        source_id = stable_ref("connector_source", "pubmed", source_ref)
        hit_id = stable_ref("source_hit", "pubmed", source_ref, title, pmid)
        doi = _article_id(article, "doi")
        metadata = [f"pmid:{pmid}"]
        if doi:
            metadata.append("doi:" + identifier(doi, 80))
        hits.append(SourceHit(
            connector_id="pubmed",
            hit_id=hit_id,
            source_ref=source_ref,
            source_id=source_id,
            title=title,
            snippet=abstract,
            content_kind="abstract",
            source_kind="biomedical_literature",
            canonical_url=canonical,
            published_at=_pubmed_year(article),
            metadata_refs=tuple(metadata),
            score=max(0.0, 1.0 - (len(hits) * 0.05)),
        ))
    return source_result_from_hits("pubmed", query=query, hits=hits)


def fetch_recorded_hit(hit: SourceHit) -> FetchedSource:
    connector_id = _connector_id(hit.connector_id)
    if connector_id not in {"arxiv", "pubmed"}:
        raise ValueError("recorded hit fetch only supports arxiv and pubmed")
    if not hit.canonical_url:
        raise ValueError("recorded hit has no canonical URL")
    reason = check_fetch_url(hit.canonical_url, resolve=False)
    reason = reason or _connector_url_denial_reason(connector_id, hit.canonical_url)
    if reason:
        raise ValueError(reason)
    if connector_id == "pubmed" and not is_valid_pubmed_id(_pubmed_id(hit.canonical_url)):
        raise ValueError("recorded PubMed hit has invalid PMID")
    if connector_id == "arxiv" and not is_valid_arxiv_id(_arxiv_id(hit.canonical_url)):
        raise ValueError("recorded arXiv hit has invalid arXiv ID")
    text = _clean_text(hit.snippet)
    if not text:
        text = _clean_text(hit.title)
    document = SourceDocument(
        requested_url=hit.canonical_url,
        final_url=hit.canonical_url,
        title=hit.title,
        content_kind=hit.content_kind or "abstract",
        mime_type="application/xml",
        text=text,
        truncated=False,
    )
    return FetchedSource.from_document(
        connector_id=connector_id,
        source_ref=hit.source_ref,
        source_id=hit.source_id,
        document=document,
    )


def _resolved_root(path: str | Path) -> Path:
    root = Path(path).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError("allowed root is not a directory")
    return root


def _read_limited_bytes(path: Path, *, max_bytes: int) -> bytes:
    size = path.stat().st_size
    if size > max(1, int(max_bytes)):
        raise ValueError("source file is too large")
    return path.read_bytes()


def _read_csv_rows(text: str, *, delimiter: str, max_rows: int) -> list[list[str]]:
    rows: list[list[str]] = []
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    row_limit = _positive_int(max_rows, MAX_TABLE_ROWS)
    for row in reader:
        rows.append([clip(cell, 120) for cell in row])
        if len(rows) >= row_limit:
            break
    return rows


def _render_table_rows(rows: list[list[str]], *, delimiter: str) -> str:
    if not rows:
        return "empty table"
    separator = "\\t" if delimiter == "\t" else ","
    headers = rows[0]
    lines = [
        "table source",
        f"delimiter: {separator}",
        "columns: " + " | ".join(headers),
    ]
    for index, row in enumerate(rows[1:], 1):
        cells = []
        for col_index, value in enumerate(row):
            name = headers[col_index] if col_index < len(headers) and headers[col_index] else f"col{col_index + 1}"
            cells.append(f"{name}={value}")
        lines.append(f"{index}. " + "; ".join(cells))
    return "\n".join(lines)


def _source_ref_for_path(connector_id: str, path: Path, root: Path) -> str:
    ref = path_ref(path, project=root)
    return stable_ref("source_ref", connector_id, ref.get("digest", ""), ref.get("basename", ""))


def _source_ref_for_url(connector_id: str, url: str) -> str:
    ref = sanitize_research_url_ref(url)
    return stable_ref("source_ref", connector_id, ref.get("url_digest", ""), ref.get("host", ""))


def _connector_url_denial_reason(connector_id: str, url: str) -> str:
    allowed = _CONNECTOR_ALLOWED_HOSTS.get(_connector_id(connector_id), frozenset())
    if not allowed:
        return ""
    host = _url_host(url)
    if host not in allowed:
        return "connector URL host is not allowed"
    return ""


def _connector_canonical_url(connector_id: str, url: str) -> str:
    text = str(url or "").strip()
    if _connector_id(connector_id) == "arxiv" and _url_host(text) == "arxiv.org":
        try:
            parsed = urlparse(text)
        except ValueError:
            return text
        return parsed._replace(scheme="https", netloc="arxiv.org").geturl()
    return text


def _connector_locator(connector_id: str, source_ref: str, basename: str) -> str:
    return f"codey-source://{_connector_id(connector_id)}/{quote(source_ref, safe='')}/{quote(basename)}"


def _xml_text(element: ET.Element, path: str) -> str:
    found = element.find(path, _ATOM_NS)
    return _clean_text(found.text if found is not None else "")


def _first_text(element: ET.Element, path: str) -> str:
    found = element.find(path)
    return _clean_text("".join(found.itertext()) if found is not None else "")


def _abstract_text(article: ET.Element) -> str:
    parts = []
    for item in article.findall(".//AbstractText"):
        label = str(item.attrib.get("Label") or "").strip()
        text = _clean_text("".join(item.itertext()))
        if not text:
            continue
        parts.append(f"{label}: {text}" if label else text)
    return _clean_text(" ".join(parts))


def _entry_link(entry: ET.Element, *, rel: str) -> str:
    for link in entry.findall("atom:link", _ATOM_NS):
        if str(link.attrib.get("rel") or "alternate") == rel:
            return str(link.attrib.get("href") or "").strip()
    return ""


def _article_id(article: ET.Element, id_type: str) -> str:
    expected = str(id_type or "").casefold()
    for item in article.findall(".//ArticleId"):
        if str(item.attrib.get("IdType") or "").casefold() == expected:
            return _clean_text(item.text)
    return ""


def _pubmed_year(article: ET.Element) -> str:
    year = _first_text(article, ".//PubDate/Year")
    if year:
        return year
    return _first_text(article, ".//ArticleDate/Year")


def _pubmed_id(url: str) -> str:
    try:
        parsed = urlparse(str(url or "").strip())
    except ValueError:
        return ""
    parts = [item for item in parsed.path.split("/") if item]
    return parts[0] if parts else ""


def _arxiv_id(url: str) -> str:
    text = str(url or "").rstrip("/")
    if "/abs/" in text:
        return text.rsplit("/abs/", 1)[1]
    return text.rsplit("/", 1)[-1]


def _url_host(url: str) -> str:
    try:
        return (urlparse(str(url or "")).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""


def _clean_text(value: object) -> str:
    return _SPACE_RE.sub(" ", str(value or "")).strip()


def _bounded_mapping(value: Mapping[str, object]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key in sorted(value):
        clean_key = _connector_id(key)
        clean_value = _safe_payload_code(value.get(key), 120)
        if looks_sensitive_code(clean_key):
            clean_key = ""
        if clean_key and clean_value:
            out[clean_key] = clean_value
    return out


def _safe_connector_code(value: object) -> str:
    raw = str(value or "")
    stripped = raw.strip()
    text = _connector_id(stripped)
    if not text or raw != stripped or text != stripped or looks_sensitive_code(text):
        return ""
    return text


def _safe_payload_refs(values: Iterable[object], *, limit: int) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        values = (values,)
    refs: list[str] = []
    seen: set[str] = set()
    for value in values or ():
        text = _safe_payload_code(value, 120)
        if not text or text in seen:
            continue
        seen.add(text)
        refs.append(text)
        if len(refs) >= limit:
            break
    return tuple(refs)


def _safe_payload_code(value: object, limit: int) -> str:
    raw = clip(value, limit)
    if not raw or looks_sensitive_code(raw):
        return ""
    text = identifier(raw, limit)
    if not text or looks_sensitive_code(text):
        return ""
    return text


def _connector_code_or_raise(value: object, label: str) -> None:
    if not _safe_connector_code(value):
        raise ValueError(f"{label} must be canonical non-sensitive snake_case")


def _connector_status(value: object) -> str:
    text = _connector_id(value)
    if text in {
        CONNECTOR_STATUS_AVAILABLE,
        CONNECTOR_STATUS_FIXTURE,
        CONNECTOR_STATUS_EXPERIMENTAL,
        CONNECTOR_STATUS_OPTIONAL,
        CONNECTOR_STATUS_UNAVAILABLE,
    }:
        return text
    raise ValueError("unknown source connector status")


def _score(value: object) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, round(score, 3)))


def _positive_int(value: object, default: int) -> int:
    if isinstance(value, bool):
        return max(1, int(default or 1))
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(default or 1)
    return max(1, parsed)


__all__ = [
    "CONNECTOR_AVAILABLE_STATUSES",
    "CONNECTOR_STATUS_AVAILABLE",
    "CONNECTOR_STATUS_EXPERIMENTAL",
    "CONNECTOR_STATUS_FIXTURE",
    "CONNECTOR_STATUS_OPTIONAL",
    "CONNECTOR_STATUS_UNAVAILABLE",
    "FetchedSource",
    "MAX_CONNECTOR_HITS",
    "SafeConnectorQuery",
    "SourceConnectorRegistry",
    "SourceConnectorResult",
    "SourceConnectorSpec",
    "SourceHit",
    "connector_query_has_secret_signal",
    "built_in_connector_registry",
    "fetch_csv_tsv_file",
    "fetch_json_file",
    "fetch_local_file",
    "fetch_recorded_hit",
    "is_valid_arxiv_id",
    "is_valid_pubmed_id",
    "parse_arxiv_atom_fixture",
    "parse_pubmed_fixture",
    "resolve_local_source_path",
    "safe_connector_query",
    "safe_connector_query_terms",
    "safe_connector_signal_text",
    "source_result_from_hits",
]
