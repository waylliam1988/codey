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

from codey.research.source_document import SourceDocument
from codey.research import source_domains

MAX_SNIPPET_CHARS = 360
MAX_CLAIM_CHARS = 260
MAX_RESULTS_PER_QUERY = 8
MAX_PAYLOAD_SEARCHES = 12
MAX_PAYLOAD_RESULTS = 48
MAX_PAYLOAD_SOURCES = 24
MAX_PAYLOAD_EVIDENCE = 48
MAX_PAYLOAD_SOURCE_SEARCHES = 24
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
    content_kind: str = "html"
    mime_type: str = ""
    page_count: int = 0
    pages_read: tuple[int, ...] = ()
    truncated: bool = False

    def to_dict(self) -> dict:
        data = asdict(self)
        data["quality"] = self.quality.to_dict()
        data["pages_read"] = list(self.pages_read)
        return data


@dataclass(frozen=True)
class EvidenceItem:
    claim: str
    source_url: str
    excerpt: str
    stance: str = "supports"
    note_id: str = ""
    page: int | None = None
    locator: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class EvidencePreparation:
    items: tuple[EvidenceItem, ...] = ()
    error: str = ""
    warning: str = ""


class ResearchLedger:
    def __init__(self) -> None:
        self.searches: list[SearchRecord] = []
        self.opened_sources: list[OpenedSource] = []
        self.evidence_items: list[EvidenceItem] = []
        self.source_searches: list[dict] = []
        self._source_texts: dict[str, str] = {}
        self._source_pages: dict[str, dict[int, str]] = {}

    def clone(self) -> ResearchLedger:
        forked = ResearchLedger()
        forked.searches = list(self.searches)
        forked.opened_sources = list(self.opened_sources)
        forked.evidence_items = list(self.evidence_items)
        forked.source_searches = [dict(s) for s in self.source_searches]
        forked._source_texts = dict(self._source_texts)
        forked._source_pages = {k: dict(v) for k, v in self._source_pages.items()}
        return forked

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
        self.record_open_document(SourceDocument.html(
            requested_url=requested_url,
            final_url=final_url,
            title=title,
            text=text,
        ))

    def record_open_document(self, document: SourceDocument) -> None:
        requested_url = str(document.requested_url or "").strip()
        final_url = str(document.final_url or requested_url).strip()
        if not final_url:
            return
        text = str(document.text or "")
        pages_read = tuple(int(page) for page in document.pages_read if int(page) > 0)
        requested_url = str(requested_url or "").strip()
        page_map = {page.number: page.text for page in document.page_texts if page.number > 0}
        existing = next(
            (
                item for item in self.opened_sources
                if item.final_url == final_url
                or item.requested_url == final_url
                or (requested_url and item.final_url == requested_url)
                or (requested_url and item.requested_url == requested_url)
            ),
            None,
        )
        if existing is not None and (existing.content_kind == "pdf" or document.content_kind == "pdf"):
            existing_pages = tuple(int(page) for page in existing.pages_read if int(page) > 0)
            pages_read = tuple(sorted({*existing_pages, *pages_read}))
            existing_text = self._source_texts.get(existing.final_url) or ""
            text = _merge_text(existing_text, text)
            merged_page_map = dict(self._source_pages.get(existing.final_url) or {})
            merged_page_map.update(page_map)
            page_map = merged_page_map
        opened = OpenedSource(
            requested_url=(existing.requested_url if existing is not None else "") or requested_url or final_url,
            final_url=final_url,
            title=_clip(str(document.title or (existing.title if existing is not None else "")), 180),
            retrieved_at=now_iso(),
            text_hash=hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16],
            quality=classify_source_quality(final_url, text),
            content_kind=str(document.content_kind or "html"),
            mime_type=str(document.mime_type or (existing.mime_type if existing is not None else "")),
            page_count=max(
                max(0, int(document.page_count or 0)),
                max(0, int(existing.page_count or 0)) if existing is not None else 0,
            ),
            pages_read=pages_read,
            truncated=bool(document.truncated or (existing.truncated if existing is not None else False)),
        )
        self.opened_sources = [
            item for item in self.opened_sources
            if item.final_url != opened.final_url
            and item.requested_url != opened.requested_url
            and item.final_url != opened.requested_url
            and item.requested_url != opened.final_url
        ]
        self.opened_sources.append(opened)
        self._source_texts[final_url] = text
        if requested_url:
            self._source_texts[requested_url] = text
        if page_map:
            self._source_pages[final_url] = page_map
            if requested_url:
                self._source_pages[requested_url] = page_map

    def prepare_evidence_items(
        self,
        evidence: object,
        *,
        fallback_sources: list[str],
        fallback_claim: str,
        fallback_body: str,
        note_type: str,
    ) -> EvidencePreparation:
        explicit = _coerce_evidence(evidence)
        if explicit:
            prepared: list[EvidenceItem] = []
            warnings: list[str] = []
            for raw in explicit:
                source = str(raw.get("source_url") or raw.get("source") or "").strip()
                if not source:
                    return EvidencePreparation(error="evidence needs source_url for each item")
                final_url = self.canonical_opened_url(source)
                if not final_url:
                    return EvidencePreparation(error=f"evidence cites a source you did not open: {source or '(missing source_url)'}")
                excerpt = str(raw.get("excerpt") or raw.get("quote") or "").strip()
                page = _as_page(raw.get("page") or raw.get("p"))
                if page is not None and not self.page_was_read(final_url, page):
                    return EvidencePreparation(error=f"evidence cites unread PDF page p.{page}: {final_url}")
                inferred_page = self.page_for_excerpt(final_url, excerpt) if excerpt else None
                if page is not None and excerpt and not self.excerpt_in_source(final_url, excerpt, page=page):
                    inferred_page = None
                if not excerpt or not self.excerpt_in_source(final_url, excerpt, page=page):
                    replacement, replacement_page = self.best_excerpt_with_page(
                        final_url,
                        fallback_body or fallback_claim,
                        page=page,
                    )
                    if not replacement:
                        return EvidencePreparation(
                            error=(
                                "evidence excerpt is not present in opened source and could not "
                                f"attach a replacement excerpt: {final_url}"
                            )
                        )
                    warnings.append(
                        "supplied evidence excerpt did not match opened source; "
                        "attached an exact opened-page excerpt instead"
                    )
                    excerpt = replacement
                    inferred_page = replacement_page
                page = page if page is not None else inferred_page
                prepared.append(EvidenceItem(
                    claim=_clip(str(raw.get("claim") or fallback_claim), MAX_CLAIM_CHARS),
                    source_url=final_url,
                    excerpt=_clip(excerpt, MAX_SNIPPET_CHARS),
                    stance=_normalize_stance(str(raw.get("stance") or "supports")),
                    page=page,
                    locator=_locator(page),
                ))
            return EvidencePreparation(tuple(prepared), warning="; ".join(dict.fromkeys(warnings)))

        prepared = []
        fallback_stance = "context" if note_type == "question" else "supports"
        for source in fallback_sources:
            if not source.startswith(("http://", "https://")):
                continue
            final_url = self.canonical_opened_url(source)
            if not final_url:
                continue
            excerpt, page = self.best_excerpt_with_page(final_url, fallback_body or fallback_claim)
            prepared.append(EvidenceItem(
                claim=_clip(fallback_claim or fallback_body, MAX_CLAIM_CHARS),
                source_url=final_url,
                excerpt=excerpt,
                stance=fallback_stance,
                page=page,
                locator=_locator(page),
            ))
        return EvidencePreparation(tuple(prepared))

    def add_evidence_items(self, items: list[EvidenceItem], *, note_id: str = "") -> None:
        for item in items:
            if not item.source_url:
                continue
            self.evidence_items.append(replace(item, note_id=note_id or item.note_id))

    def record_source_search(self, source_url: str, query: str, hits: list[dict]) -> None:
        final_url = self.canonical_opened_url(source_url) or str(source_url or "").strip()
        query = " ".join(str(query or "").split())
        if not final_url or not query:
            return
        bounded_hits = []
        for hit in hits[:MAX_PAYLOAD_SOURCE_SEARCHES]:
            bounded_hits.append({
                "page": hit.get("page"),
                "offset": int(hit.get("offset") or 0),
                "snippet": _clip(str(hit.get("snippet") or ""), MAX_SNIPPET_CHARS),
            })
        self.source_searches.append({
            "source_url": final_url,
            "query": query,
            "hits": bounded_hits[:12],
        })

    def source_record_for_url(self, source_url: str) -> OpenedSource | None:
        final_url = self.canonical_opened_url(source_url) or str(source_url or "").strip()
        if not final_url:
            return None
        for item in self.opened_sources:
            if item.final_url == final_url or item.requested_url == final_url:
                return item
        return None

    def source_text_for_url(self, source_url: str) -> str:
        final_url = self.canonical_opened_url(source_url) or str(source_url or "").strip()
        return self._source_texts.get(final_url) or self._source_texts.get(str(source_url or "").strip()) or ""

    def source_pages_for_url(self, source_url: str) -> dict[int, str]:
        final_url = self.canonical_opened_url(source_url) or str(source_url or "").strip()
        return dict(self._source_pages.get(final_url) or self._source_pages.get(str(source_url or "").strip()) or {})

    def best_excerpt(self, source_url: str, hint: str = "") -> str:
        excerpt, _page = self.best_excerpt_with_page(source_url, hint)
        return excerpt

    def best_excerpt_with_page(self, source_url: str, hint: str = "", *, page: int | None = None) -> tuple[str, int | None]:
        if page is not None:
            page_text = (self._source_pages.get(source_url) or {}).get(page, "")
            return (_best_excerpt(page_text, hint), page) if page_text else ("", None)
        for page_number, page_text in (self._source_pages.get(source_url) or {}).items():
            excerpt = _best_excerpt(page_text, hint)
            if excerpt:
                return excerpt, page_number
        text = self._source_texts.get(source_url) or ""
        return _best_excerpt(text, hint), None

    def excerpt_in_source(self, source_url: str, excerpt: str, *, page: int | None = None) -> bool:
        text = (
            (self._source_pages.get(source_url) or {}).get(page, "")
            if page is not None
            else self._source_texts.get(source_url) or ""
        )
        if not text:
            return False
        excerpt_norm = _normalize_for_match(excerpt)
        if len(excerpt_norm) < 12:
            return False
        return excerpt_norm in _normalize_for_match(text)

    def page_for_excerpt(self, source_url: str, excerpt: str) -> int | None:
        if not excerpt:
            return None
        for page, text in (self._source_pages.get(source_url) or {}).items():
            if self.excerpt_in_source(source_url, excerpt, page=page):
                return page
        return None

    def page_was_read(self, source_url: str, page: int) -> bool:
        final_url = self.canonical_opened_url(source_url) or str(source_url or "")
        return page in self.pages_read_for_url(final_url)

    def pages_read_for_url(self, source_url: str) -> set[int]:
        final_url = self.canonical_opened_url(source_url) or str(source_url or "")
        for item in self.opened_sources:
            if item.final_url == final_url:
                return set(item.pages_read)
        return set()

    def evidence_pages_for_url(self, source_url: str) -> set[int]:
        final_url = self.canonical_opened_url(source_url) or str(source_url or "")
        return {
            int(item.page)
            for item in self.evidence_items
            if item.source_url == final_url and item.page is not None and item.excerpt.strip()
        }

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
            "source_searches": list(self.source_searches[:MAX_PAYLOAD_SOURCE_SEARCHES]),
            "opened_count": len(self.opened_sources),
            "skipped_results": skipped,
        }


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().replace(microsecond=0).isoformat()


def classify_source_quality(url: str, text: str = "") -> SourceQuality:
    parsed = urlparse(str(url or ""))
    host = source_domains.strip_www(parsed.hostname)
    path = (parsed.path or "").lower()
    group = _independent_group(host)
    kind = "web"
    level = "secondary"
    if source_domains.matches_any(host, source_domains.DATASET_HOSTS):
        # Most specific first: data.gov / data.nasa.gov are dataset
        # repositories that also sit on gov suffixes; the repository role is
        # the meaningful quality stamp, so it wins over the TLD shape.
        kind = "data"
        level = "primary"
    elif source_domains.is_government_host(host):
        # Strong trust is granted only by registered-suffix host shapes;
        # lookalikes (sec.gov.evil.example) stay plain web/secondary.
        kind = "official"
        level = "primary"
    elif source_domains.is_education_host(host):
        kind = "data"
        level = "primary"
    elif source_domains.matches_any(host, source_domains.NEWS_HOSTS):
        kind = "media"
    elif source_domains.matches_any(host, source_domains.BLOG_HOSTS):
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
    return normalize_evidence_stance(value)


def normalize_evidence_stance(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return "supports"
    normalized = re.sub(r"[^a-z0-9]+", "_", text.casefold()).strip("_")
    if normalized in {
        "contradicts",
        "contradict",
        "contradicting",
        "contradiction",
        "counter",
        "against",
        "opposes",
        "oppose",
        "opposed",
        "refutes",
        "refute",
        "refuting",
        "refutation",
        "rebuttal",
    }:
        return "contradicts"
    if normalized in {
        "context",
        "background",
        "limits",
        "limitation",
        "limitations",
        "qualified",
        "qualifies",
    }:
        return "context"
    if normalized in {"supports", "support", "supporting", "for", "confirms", "confirm"}:
        return "supports"
    return "unknown"


def _as_page(value: object) -> int | None:
    if value in (None, ""):
        return None
    match = re.search(r"\d+", str(value))
    if not match:
        return None
    number = int(match.group(0))
    return number if number > 0 else None


def _locator(page: int | None) -> str:
    return f"p.{page}" if page is not None and page > 0 else ""


def _best_excerpt(text: str, hint: str = "") -> str:
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


def _merge_text(existing: str, current: str) -> str:
    left = str(existing or "").strip()
    right = str(current or "").strip()
    if not left:
        return right
    if not right or right in left:
        return left
    if left in right:
        return right
    return f"{left}\n\n{right}"


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
