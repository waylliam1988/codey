"""Runtime evidence ledger for one Research run.

The Markdown vault remains the long-term source of truth. This module keeps the
more detailed, per-run audit trail that the UI and report validator need:
searches, opened final URLs, source quality hints, and short evidence snippets.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from urllib.parse import urlparse

MAX_SNIPPET_CHARS = 360
MAX_CLAIM_CHARS = 260
MAX_RESULTS_PER_QUERY = 8
MAX_PAYLOAD_SEARCHES = 12
MAX_PAYLOAD_RESULTS = 48
MAX_PAYLOAD_SOURCES = 24
MAX_PAYLOAD_EVIDENCE = 48
_SPACE_RE = re.compile(r"\s+")
_YEAR_RE = re.compile(r"\b(20\d{2})\b")


@dataclass(frozen=True)
class SourceQuality:
    level: str = "secondary"
    kind: str = "web"
    freshness: str = "undated"
    independent_group: str = ""

    def render(self) -> str:
        parts = [self.level, self.kind, self.freshness, self.independent_group]
        return " · ".join(part for part in parts if part)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class SearchResultRecord:
    rank: int
    title: str
    url: str
    snippet: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class SearchRecord:
    query: str
    timestamp: str
    results: tuple[SearchResultRecord, ...] = ()

    def to_dict(self) -> dict:
        data = asdict(self)
        data["results"] = [item.to_dict() for item in self.results]
        return data


@dataclass(frozen=True)
class OpenedSource:
    requested_url: str
    final_url: str
    title: str
    retrieved_at: str
    text_hash: str
    quality: SourceQuality = field(default_factory=SourceQuality)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["quality"] = self.quality.to_dict()
        return data


@dataclass(frozen=True)
class EvidenceItem:
    claim: str
    source_url: str
    excerpt: str
    stance: str = "supports"
    note_id: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class ResearchLedger:
    def __init__(self) -> None:
        self.searches: list[SearchRecord] = []
        self.opened_sources: list[OpenedSource] = []
        self.evidence_items: list[EvidenceItem] = []
        self._source_texts: dict[str, str] = {}

    def record_search(self, query: str, results: list[dict]) -> None:
        query = " ".join(str(query or "").split())
        if not query:
            return
        items: list[SearchResultRecord] = []
        for index, result in enumerate(results[:MAX_RESULTS_PER_QUERY], 1):
            url = str(result.get("url") or "").strip()
            if not url:
                continue
            items.append(SearchResultRecord(
                rank=index,
                title=_clip(str(result.get("title") or ""), 160),
                url=url,
                snippet=_clip(str(result.get("snippet") or ""), 260),
            ))
        self.searches.append(SearchRecord(query=query, timestamp=now_iso(), results=tuple(items)))

    def record_open(self, requested_url: str, final_url: str, title: str, text: str) -> None:
        requested_url = str(requested_url or "").strip()
        final_url = str(final_url or requested_url).strip()
        if not final_url:
            return
        text = str(text or "")
        opened = OpenedSource(
            requested_url=requested_url or final_url,
            final_url=final_url,
            title=_clip(str(title or ""), 180),
            retrieved_at=now_iso(),
            text_hash=hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16],
            quality=classify_source_quality(final_url, text),
        )
        self.opened_sources = [
            item for item in self.opened_sources
            if item.final_url != opened.final_url and item.requested_url != opened.requested_url
        ]
        self.opened_sources.append(opened)
        self._source_texts[final_url] = text
        if requested_url:
            self._source_texts[requested_url] = text

    def prepare_evidence_items(
        self,
        evidence: object,
        *,
        fallback_sources: list[str],
        fallback_claim: str,
        fallback_body: str,
        note_type: str,
    ) -> tuple[list[EvidenceItem], str | None]:
        explicit = _coerce_evidence(evidence)
        if explicit:
            prepared: list[EvidenceItem] = []
            for raw in explicit:
                source = str(raw.get("source_url") or raw.get("source") or "").strip()
                if not source:
                    return [], "evidence needs source_url for each item"
                final_url = self.canonical_opened_url(source)
                if not final_url:
                    return [], f"evidence cites a source you did not open: {source or '(missing source_url)'}"
                excerpt = str(raw.get("excerpt") or raw.get("quote") or "").strip()
                if not excerpt:
                    return [], "evidence excerpt is required"
                if not self.excerpt_in_source(final_url, excerpt):
                    return [], f"evidence excerpt is not present in opened source: {final_url}"
                prepared.append(EvidenceItem(
                    claim=_clip(str(raw.get("claim") or fallback_claim), MAX_CLAIM_CHARS),
                    source_url=final_url,
                    excerpt=_clip(excerpt, MAX_SNIPPET_CHARS),
                    stance=_normalize_stance(str(raw.get("stance") or "supports")),
                ))
            return prepared, None

        prepared = []
        fallback_stance = "context" if note_type == "question" else "supports"
        for source in fallback_sources:
            if not source.startswith(("http://", "https://")):
                continue
            final_url = self.canonical_opened_url(source)
            if not final_url:
                continue
            prepared.append(EvidenceItem(
                claim=_clip(fallback_claim or fallback_body, MAX_CLAIM_CHARS),
                source_url=final_url,
                excerpt=self.best_excerpt(final_url, fallback_body or fallback_claim),
                stance=fallback_stance,
            ))
        return prepared, None

    def add_evidence_items(self, items: list[EvidenceItem], *, note_id: str = "") -> None:
        for item in items:
            if not item.source_url:
                continue
            self.evidence_items.append(replace(item, note_id=note_id or item.note_id))

    def best_excerpt(self, source_url: str, hint: str = "") -> str:
        text = self._source_texts.get(source_url) or ""
        if not text:
            return ""
        clean = _clean(text)
        if not clean:
            return ""
        lowered = clean.lower()
        for token in _hint_tokens(hint):
            index = lowered.find(token.lower())
            if index >= 0:
                start = max(0, index - 80)
                return _clip(clean[start : start + MAX_SNIPPET_CHARS], MAX_SNIPPET_CHARS)
        return _clip(clean, MAX_SNIPPET_CHARS)

    def excerpt_in_source(self, source_url: str, excerpt: str) -> bool:
        text = self._source_texts.get(source_url) or ""
        if not text:
            return False
        excerpt_norm = _normalize_for_match(excerpt)
        if len(excerpt_norm) < 12:
            return False
        return excerpt_norm in _normalize_for_match(text)

    def canonical_opened_url(self, url: str) -> str:
        url = str(url or "").strip()
        if not url:
            return ""
        for item in self.opened_sources:
            if url in {item.final_url, item.requested_url}:
                return item.final_url
        return ""

    def final_url_set(self) -> set[str]:
        return {item.final_url for item in self.opened_sources if item.final_url}

    def quality_for_url(self, url: str) -> SourceQuality:
        final_url = self.canonical_opened_url(url) or str(url or "")
        for item in self.opened_sources:
            if item.final_url == final_url:
                return item.quality
        return classify_source_quality(final_url, "")

    def source_title(self, url: str) -> str:
        final_url = self.canonical_opened_url(url) or str(url or "")
        for item in self.opened_sources:
            if item.final_url == final_url:
                return item.title
        return ""

    def search_results_payload(self) -> list[dict]:
        opened = self.final_url_set()
        rows: list[dict] = []
        for search in self.searches[:MAX_PAYLOAD_SEARCHES]:
            for result in search.results:
                final = self.canonical_opened_url(result.url)
                rows.append({
                    "query": search.query,
                    "rank": result.rank,
                    "title": result.title,
                    "url": result.url,
                    "snippet": result.snippet,
                    "opened": bool(final and final in opened),
                    "final_url": final,
                })
                if len(rows) >= MAX_PAYLOAD_RESULTS:
                    return rows
        return rows

    def opened_sources_payload(self) -> list[dict]:
        return [item.to_dict() for item in self.opened_sources[:MAX_PAYLOAD_SOURCES]]

    def evidence_payload(self) -> list[dict]:
        return [item.to_dict() for item in self.evidence_items[:MAX_PAYLOAD_EVIDENCE]]

    def coverage_payload(self) -> dict:
        opened_final = self.final_url_set()
        opened_requested = {
            item.requested_url for item in self.opened_sources if item.requested_url
        }
        skipped: list[dict] = []
        for search in self.searches:
            for result in search.results:
                final = self.canonical_opened_url(result.url)
                if result.url in opened_requested or final in opened_final:
                    continue
                skipped.append({
                    "query": search.query,
                    "rank": result.rank,
                    "title": result.title,
                    "url": result.url,
                    "reason": "not opened",
                })
                if len(skipped) >= 16:
                    break
            if len(skipped) >= 16:
                break
        return {
            "queries": [item.query for item in self.searches[:MAX_PAYLOAD_SEARCHES]],
            "searches": [item.to_dict() for item in self.searches[:MAX_PAYLOAD_SEARCHES]],
            "opened_count": len(self.opened_sources),
            "skipped_results": skipped,
        }


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().replace(microsecond=0).isoformat()


def classify_source_quality(url: str, text: str = "") -> SourceQuality:
    parsed = urlparse(str(url or ""))
    host = (parsed.hostname or "").lower().removeprefix("www.")
    path = (parsed.path or "").lower()
    group = _independent_group(host)
    kind = "web"
    level = "secondary"
    if host.endswith(".gov") or ".gov." in host:
        kind = "official"
        level = "primary"
    elif host.endswith(".edu") or ".edu." in host:
        kind = "data"
        level = "primary"
    elif any(marker in host for marker in ("reuters", "apnews", "bbc", "news", "nytimes", "wsj")):
        kind = "media"
    elif any(marker in host for marker in ("blog", "medium", "substack")):
        kind = "blog"
    elif any(marker in path for marker in ("data", "dataset", "statistics", "report", "download")):
        kind = "data"
    freshness = _freshness(text or url)
    return SourceQuality(level=level, kind=kind, freshness=freshness, independent_group=group)


def _freshness(text: str) -> str:
    years = []
    for match in _YEAR_RE.findall(str(text or "")):
        try:
            years.append(int(match))
        except ValueError:
            pass
    if not years:
        return "undated"
    current = datetime.now(timezone.utc).year
    latest = max(years)
    if latest >= current - 2:
        return "fresh"
    return "stale"


def _independent_group(host: str) -> str:
    parts = [part for part in str(host or "").split(".") if part]
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return host


def _coerce_evidence(value: object) -> list[dict]:
    if isinstance(value, dict):
        return [value]
    if isinstance(value, (list, tuple)):
        return [item for item in value if isinstance(item, dict)]
    return []


def _normalize_stance(value: str) -> str:
    text = (value or "").strip().lower()
    if text in {"contradicts", "contradict", "counter", "against"}:
        return "contradicts"
    if text in {"context", "background", "limits", "limitation"}:
        return "context"
    return "supports"


def _hint_tokens(hint: str) -> list[str]:
    tokens = []
    for raw in re.findall(r"[A-Za-z0-9\u4e00-\u9fff]{3,}", str(hint or "").lower()):
        if len(raw) >= 24:
            raw = raw[:24]
        if raw not in tokens:
            tokens.append(raw)
    return tokens[:8]


def _normalize_for_match(value: str) -> str:
    return _SPACE_RE.sub(" ", str(value or "")).strip().lower()


def _clean(value: str) -> str:
    return _SPACE_RE.sub(" ", str(value or "")).strip()


def _clip(value: str, limit: int) -> str:
    text = _clean(value)
    if len(text) <= limit:
        return text
    if limit <= 12:
        return text[:limit]
    return text[: limit - 3].rstrip() + "..."
