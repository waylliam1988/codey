"""Single orchestrator for Research source acquisition.

Three verbs, nothing else: ``search`` (connector-first, browser fallback),
``open`` (fetch policy, fetch, redirect checks, PDF/HTML build, opened-ledger
record), ``search_inside`` (ledger lookup, PDF page scan, in-source search).

``source_connectors.py`` stays the pure connector contract (specs, hits,
fetch of recorded hits) and ``browser_search.py`` stays the browser runtime;
neither imports the other and neither knows about the ledger. All
cross-cutting decisions -- which provider to try, what counts as opened,
which failures are recorded -- live here.

``ResearchTools`` keeps model-visible rendering (windows, markdown, the
exact ``ERROR:``/``SKIPPED:`` strings) plus the diagnostics sink, and
delegates acquisition to a gateway built from its own provider and ledger.
Outcomes are frozen dataclasses so tools can map them to strings without
re-implementing policy; behavior modules must not import run projections
(``runs.trace`` etc.), and this module does not.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass

from codey.research.ledger import ResearchLedger
from codey.research.pdf_extract import (
    PDF_DEFAULT_PAGES,
    PDF_MAX_PAGES_PER_OPEN,
    PdfSkipped,
    extract_pdf_document,
)
from codey.research.source_document import SourceDocument
from codey.research.source_search import (
    SOURCE_SEARCH_DEFAULT_LIMIT,
    SourceSearchHit,
    bounded_limit,
    search_pages,
    search_text,
)
from codey.research.url_selection import source_candidate_skip_reason
from codey.policies.network import check_fetch_url
from codey.runtime.core import cancellation

OPEN_DEFAULT_LIMIT = 6000
OPEN_MAX_LIMIT = 12000
OPEN_MIN_LIMIT = 500
SEARCH_LIMIT = 8
PDF_SOURCE_SEARCH_MAX_PAGES = 30


@dataclass(frozen=True)
class SearchedSources:
    """Outcome of ``search``: raw provider hits, or an error string."""

    query: str
    hits: tuple[dict, ...] = ()
    error: str = ""


@dataclass(frozen=True)
class OpenedSource:
    """Outcome of ``open``: a built document, or a skip/error detail.

    ``detail`` never carries the ``ERROR:``/``SKIPPED:`` prefix; tools add
    it when rendering, so the status contract stays a plain comparison.
    """

    status: str  # "ok" | "skipped" | "error"
    detail: str = ""
    document: SourceDocument | None = None
    read_urls: tuple[str, ...] = ()


@dataclass(frozen=True)
class SearchedInside:
    """Outcome of ``search_inside``: hits, or a status detail.

    ``detail`` never carries the ``ERROR:``/``NEEDS_OPEN:`` prefix; tools
    add it when rendering.
    """

    status: str  # "ok" | "error" | "needs_open"
    detail: str = ""
    final_url: str = ""
    hits: tuple[SourceSearchHit, ...] = ()


@dataclass
class ResearchSourceGateway:
    """Acquisition spine over one search provider and one run ledger."""

    search_provider: object
    ledger: ResearchLedger
    on_failure: Callable[..., None] | None = None

    def search(self, query: str, limit: int = SEARCH_LIMIT) -> SearchedSources:
        query = (query or "").strip()
        if not query:
            return SearchedSources(query="", error="empty query")
        cancellation.check()
        try:
            results = self.search_provider.search(query, limit=limit)
        except cancellation.TaskCancelled:
            raise
        except Exception as exc:
            self._fail("search", "search", exc)
            return SearchedSources(query=query, error=f"search failed: {exc}")
        cancellation.check()
        self.ledger.record_search(query, results)
        return SearchedSources(query=query, hits=tuple(results or ()))

    def open(
        self,
        url: str,
        *,
        offset: int = 0,
        limit: int = OPEN_DEFAULT_LIMIT,
        pages: str = "",
    ) -> OpenedSource:
        url = (url or "").strip()
        if not url:
            return OpenedSource(status="error", detail="open_url needs a url")
        skip_reason = source_candidate_skip_reason(url)
        if skip_reason:
            self._fail("browser", "open", skip_reason, url)
            return OpenedSource(
                status="skipped",
                detail=f"{skip_reason}. Choose a specific article, document, or readable source page.",
            )
        offset = max(0, _as_int(offset, 0))
        limit = min(OPEN_MAX_LIMIT, max(OPEN_MIN_LIMIT, _as_int(limit, OPEN_DEFAULT_LIMIT)))
        reason = check_fetch_url(url, use_cache=True)
        if reason:
            self._fail("browser", "open", reason, url)
            return OpenedSource(status="error", detail=reason)
        cancellation.check()
        try:
            page = self.search_provider.fetch(url)
        except cancellation.TaskCancelled:
            raise
        except Exception as exc:
            self._fail("browser", "open", exc, url)
            return OpenedSource(status="error", detail=f"open failed: {exc}")
        cancellation.check()
        page_url = str(page.get("url") or url)
        page_text = str(page.get("text") or "")
        skip_reason = source_candidate_skip_reason(page_url)
        if skip_reason:
            self._fail("browser", "open", skip_reason + " after redirect", url)
            return OpenedSource(
                status="skipped",
                detail=f"{skip_reason} after redirect. Choose a specific article, document, or readable source page.",
            )
        if page_text.startswith("SKIPPED:"):
            return OpenedSource(status="skipped", detail=page_text[len("SKIPPED:") :].strip())
        if page_text.startswith("ERROR:"):
            message = page_text[len("ERROR:") :].strip()
            if message.lower().startswith("unsupported content type:"):
                return OpenedSource(
                    status="skipped",
                    detail=f"{message}. Choose an HTML source or another readable page.",
                )
            self._fail("browser", "open", message, url)
            return OpenedSource(status="error", detail=message)
        reason = check_fetch_url(page_url, use_cache=True)
        if reason:
            self._fail("browser", "open", f"{reason} (after redirect)", page_url or url)
            return OpenedSource(status="error", detail=f"{reason} (after redirect)")
        document = self._source_document_from_fetch(url, page, pages=pages)
        if isinstance(document, PdfSkipped):
            return OpenedSource(
                status="skipped",
                detail=f"{document.reason}. Choose an HTML source or another readable PDF.",
            )
        read_urls = (url,) if not page_url or page_url == url else (url, page_url)
        self.ledger.record_open_document(document)
        return OpenedSource(status="ok", document=document, read_urls=read_urls)

    def _source_document_from_fetch(
        self, requested_url: str, page: dict, *, pages: str = ""
    ) -> SourceDocument | PdfSkipped:
        final_url = str(page.get("url") or requested_url)
        content_kind = str(page.get("content_kind") or "").lower()
        mime_type = str(page.get("mime_type") or "")
        if content_kind == "pdf":
            return extract_pdf_document(
                bytes(page.get("bytes") or b""),
                requested_url=requested_url,
                final_url=final_url,
                title=str(page.get("title") or ""),
                mime_type=mime_type or "application/pdf",
                pages=pages or PDF_DEFAULT_PAGES,
            )
        return SourceDocument.html(
            requested_url=requested_url,
            final_url=final_url,
            title=str(page.get("title") or ""),
            text=str(page.get("text") or ""),
            mime_type=mime_type or "text/html",
            truncated=bool(page.get("truncated")),
        )

    def search_inside(
        self,
        url: str,
        query: str,
        limit: object = SOURCE_SEARCH_DEFAULT_LIMIT,
    ) -> SearchedInside:
        url = (url or "").strip()
        query = (query or "").strip()
        if not url:
            return SearchedInside(status="error", detail="source_search needs a url")
        if not query:
            return SearchedInside(status="error", detail="source_search needs a query")
        final_url = self.ledger.canonical_opened_url(url)
        if not final_url:
            return SearchedInside(
                status="needs_open",
                detail="open the source before source_search: " + url,
                final_url="",
            )
        source = self.ledger.source_record_for_url(final_url)
        if source is None:
            return SearchedInside(
                status="error",
                detail="source_search source is not in the opened-source ledger",
                final_url=final_url,
            )
        hit_limit = bounded_limit(limit)
        cancellation.check()
        if source.content_kind == "pdf":
            hits = search_pages(self.ledger.source_pages_for_url(final_url), query, hit_limit)
            if self._pdf_scan_needed(source.final_url):
                hits = _merge_source_hits(
                    [*hits, *self._pdf_search_hits(source.final_url, query, hit_limit)],
                    hit_limit,
                )
        else:
            hits = search_text(self.ledger.source_text_for_url(final_url), query, hit_limit)
        self.ledger.record_source_search(final_url, query, [hit.to_dict() for hit in hits])
        return SearchedInside(status="ok", final_url=final_url, hits=tuple(hits))

    def _pdf_scan_needed(self, final_url: str) -> bool:
        source = self.ledger.source_record_for_url(final_url)
        if source is None or source.content_kind != "pdf":
            return False
        page_count = max(0, int(source.page_count or 0))
        if page_count <= 0:
            return False
        scan_end = min(page_count, PDF_SOURCE_SEARCH_MAX_PAGES)
        pages_read = self.ledger.pages_read_for_url(final_url)
        return any(page not in pages_read for page in range(1, scan_end + 1))

    def _pdf_search_hits(self, final_url: str, query: str, limit: int) -> list[SourceSearchHit]:
        source = self.ledger.source_record_for_url(final_url)
        if source is None or source.content_kind != "pdf":
            return []
        page_count = max(0, int(source.page_count or 0))
        if page_count <= 0:
            return []
        scan_end = min(page_count, PDF_SOURCE_SEARCH_MAX_PAGES)
        try:
            page = self.search_provider.fetch(source.final_url)
        except cancellation.TaskCancelled:
            raise
        except Exception as exc:
            self._fail("browser", "source_search", exc, source.final_url)
            return []
        cancellation.check()
        page_url = str(page.get("url") or source.final_url)
        reason = check_fetch_url(page_url)
        if reason:
            self._fail("browser", "source_search", reason, source.final_url)
            return []
        if page_url != source.final_url and self.ledger.canonical_opened_url(page_url) != source.final_url:
            self._fail(
                "browser", "source_search", "redirect changed opened source", source.final_url
            )
            return []
        data = bytes(page.get("bytes") or b"")
        if not data:
            return []
        page_texts: dict[int, str] = {}
        for start in range(1, scan_end + 1, PDF_MAX_PAGES_PER_OPEN):
            end = min(scan_end, start + PDF_MAX_PAGES_PER_OPEN - 1)
            document = extract_pdf_document(
                data,
                requested_url=source.requested_url or source.final_url,
                final_url=source.final_url,
                title=source.title,
                mime_type=source.mime_type or "application/pdf",
                pages=f"{start}-{end}",
            )
            if isinstance(document, PdfSkipped):
                continue
            page_texts.update({page.number: page.text for page in document.page_texts})
        return search_pages(page_texts, query, limit)

    def _fail(self, area: str, action: str, error: object, url: str = "") -> None:
        if self.on_failure is None:
            return
        with contextlib.suppress(Exception):
            self.on_failure(area, action, error, url=url)


def _merge_source_hits(hits: list[SourceSearchHit], limit: int) -> list[SourceSearchHit]:
    seen: set[tuple[int | None, int]] = set()
    unique: list[SourceSearchHit] = []
    for hit in sorted(hits, key=lambda item: (-item.score, item.page or 0, item.offset)):
        key = (hit.page, hit.offset)
        if key in seen:
            continue
        seen.add(key)
        unique.append(hit)
        if len(unique) >= limit:
            break
    return unique


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


__all__ = [
    "OPEN_DEFAULT_LIMIT",
    "OPEN_MAX_LIMIT",
    "OPEN_MIN_LIMIT",
    "PDF_SOURCE_SEARCH_MAX_PAGES",
    "SEARCH_LIMIT",
    "OpenedSource",
    "ResearchSourceGateway",
    "SearchedInside",
    "SearchedSources",
]
