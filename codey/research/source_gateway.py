"""Single orchestrator for Research source acquisition.

Three verbs, nothing else: ``search`` (connector-first, browser fallback),
``open`` (fetch policy, fetch, redirect checks, PDF/HTML build, opened-ledger
record), ``search_inside`` (ledger lookup, PDF page scan, in-source search).

``source_connectors.py`` stays the pure connector contract (specs, hits,
fetch of recorded hits) and ``browser_search.py`` stays the browser runtime;
neither imports the other and neither knows about the ledger. All
The configured provider owns backend selection and rendering upgrades;
this gateway owns source policy, document construction and opened records.

``ResearchTools`` keeps model-visible rendering (windows, markdown, the
exact ``ERROR:``/``SKIPPED:`` strings) plus the diagnostics sink, and
delegates acquisition to a gateway built from its own provider and ledger.
Outcomes are frozen dataclasses so tools can map them to strings without
re-implementing policy; behavior modules must not import run projections
(``runs.trace`` etc.), and this module does not.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from codey.policies.network import check_fetch_url
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
from codey.research.source_snapshots import captured_source, document_snapshot
from codey.research.url_selection import source_candidate_skip_reason
from codey.runtime.core import cancellation
from codey.task.web_intent import source_url_key
from codey.types.json_value import JsonObject, decode_json_object

OPEN_DEFAULT_LIMIT = 6000
OPEN_MAX_LIMIT = 12000
OPEN_MIN_LIMIT = 500
SEARCH_LIMIT = 8
PDF_SOURCE_SEARCH_MAX_PAGES = 30


@runtime_checkable
class _SearchProvider(Protocol):
    def search(self, query: str, *, limit: int) -> list[JsonObject]: ...
    def fetch(self, url: str) -> dict[str, object]: ...


class SourceFailureRecorder(Protocol):
    def __call__(self, area: str, action: str, error: object, *, url: str = "") -> None: ...


@dataclass(frozen=True)
class SearchedSources:
    """Outcome of ``search``: raw provider hits, or an error string."""

    query: str
    hits: tuple[JsonObject, ...] = ()
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
    acquisition_failed: bool = False
    acquisition: JsonObject = field(default_factory=dict)


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

    search_provider: _SearchProvider
    ledger: ResearchLedger
    on_failure: SourceFailureRecorder | None = None
    direct_urls: frozenset[str] = frozenset()

    def search(self, query: str, limit: int = SEARCH_LIMIT) -> SearchedSources:
        query = (query or "").strip()
        if not query:
            return SearchedSources(query="", error="empty query")
        cancellation.check()
        try:
            results = self.search_provider.search(query, limit=limit)
        except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
            raise
        except Exception as exc:
            self._fail("search", "search", exc)
            detail = str(exc) or type(exc).__name__
            return SearchedSources(query=query, error=f"search failed: {detail}")
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
        direct = source_url_key(url) in self.direct_urls
        skip_reason = "" if direct else source_candidate_skip_reason(url)
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
        except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
            raise
        except Exception as exc:
            self._fail("browser", "open", exc, url)
            return OpenedSource(status="error", detail=f"open failed: {exc}", acquisition_failed=True)
        cancellation.check()
        acquisition = decode_json_object(page.get("acquisition", {}))
        page_url = str(page.get("url") or url)
        status = page.get("status")
        if type(status) is not str or status not in {"ok", "error", "skipped"}:
            return OpenedSource(status="error", detail="fetch returned an invalid structured status", acquisition=acquisition)
        skip_reason = "" if direct else source_candidate_skip_reason(page_url)
        if skip_reason:
            self._fail("browser", "open", skip_reason + " after redirect", url)
            return OpenedSource(
                status="skipped",
                detail=f"{skip_reason} after redirect. Choose a specific article, document, or readable source page.",
                acquisition=acquisition,
            )
        if status != "ok":
            message = page.get("detail")
            if type(message) is not str or not message:
                return OpenedSource(status="error", detail="fetch returned an invalid failure detail", acquisition=acquisition)
            self._fail("browser", "open", message, url)
            return OpenedSource(status=status, detail=message, acquisition_failed=True, acquisition=acquisition)
        reason = check_fetch_url(page_url, use_cache=True)
        if reason:
            self._fail("browser", "open", f"{reason} (after redirect)", page_url or url)
            return OpenedSource(status="error", detail=f"{reason} (after redirect)", acquisition=acquisition)
        try:
            document = self._source_document_from_fetch(url, page, pages=pages)
        except (ValueError, TypeError) as exc:
            self._fail("browser", "open", exc, url)
            return OpenedSource(status="error", detail=f"invalid fetched document: {exc}", acquisition=acquisition)
        if isinstance(document, PdfSkipped):
            return OpenedSource(
                status="skipped",
                detail=f"{document.reason}. Choose an HTML source or another readable PDF.",
                acquisition=acquisition,
            )
        read_urls = (url,) if not page_url or page_url == url else (url, page_url)
        self.ledger.record_open_document(document)
        return OpenedSource(status="ok", document=document, read_urls=read_urls, acquisition=acquisition)

    def _source_document_from_fetch(
        self, requested_url: str, page: dict[str, object], *, pages: str = ""
    ) -> SourceDocument | PdfSkipped:
        final_url = str(page.get("url") or requested_url)
        content_kind = str(page.get("content_kind") or "").lower()
        mime_type = str(page.get("mime_type") or "")
        if content_kind == "pdf":
            return extract_pdf_document(
                _fetch_bytes(page.get("bytes")),
                requested_url=requested_url,
                final_url=final_url,
                title=str(page.get("title") or ""),
                mime_type=mime_type or "application/pdf",
                pages=pages or PDF_DEFAULT_PAGES,
            )
        return SourceDocument(
            requested_url=requested_url,
            final_url=final_url,
            title=str(page.get("title") or ""),
            content_kind="abstract" if content_kind == "abstract" else "html",
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
        snapshot = captured_source(self.ledger, final_url)
        if source.content_kind == "pdf":
            hits = search_pages(self.ledger.source_pages_for_url(final_url), query, hit_limit)
            if self._pdf_scan_needed(source.final_url):
                scanned = self._pdf_search_snapshot(source.final_url)
                if scanned is not None:
                    from codey.research.ledger_receipts import source_observation

                    _source, _text, page_map = source_observation(scanned)
                    snapshot = scanned
                    hits = search_pages(page_map, query, hit_limit)
        else:
            hits = search_text(self.ledger.source_text_for_url(final_url), query, hit_limit)
        self.ledger.record_source_search(final_url, query, [hit.to_dict() for hit in hits], source_snapshot=snapshot)
        return SearchedInside(status="ok", final_url=final_url, hits=tuple(hits))

    def _pdf_scan_needed(self, final_url: str) -> bool:
        source = self.ledger.source_record_for_url(final_url)
        if source is None or source.content_kind != "pdf":
            return False
        page_count = max(0, (source.page_count or 0))
        if page_count <= 0:
            return False
        scan_end = min(page_count, PDF_SOURCE_SEARCH_MAX_PAGES)
        pages_read = self.ledger.pages_read_for_url(final_url)
        return any(page not in pages_read for page in range(1, scan_end + 1))

    def _pdf_search_snapshot(self, final_url: str) -> JsonObject | None:
        source = self.ledger.source_record_for_url(final_url)
        if source is None or source.content_kind != "pdf":
            return None
        page_count = max(0, (source.page_count or 0))
        if page_count <= 0:
            return None
        scan_end = min(page_count, PDF_SOURCE_SEARCH_MAX_PAGES)
        try:
            page = self.search_provider.fetch(source.final_url)
        except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
            raise
        except Exception as exc:
            self._fail("browser", "source_search", exc, source.final_url)
            return None
        cancellation.check()
        if page.get("status") != "ok":
            return None
        page_url = str(page.get("url") or source.final_url)
        reason = check_fetch_url(page_url)
        if reason:
            self._fail("browser", "source_search", reason, source.final_url)
            return None
        if page_url != source.final_url and self.ledger.canonical_opened_url(page_url) != source.final_url:
            self._fail(
                "browser", "source_search", "redirect changed opened source", source.final_url
            )
            return None
        data = _fetch_bytes(page.get("bytes"))
        if not data:
            return None
        page_texts: dict[int, str] = {}
        documents: list[SourceDocument] = []
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
            documents.append(document)
            page_texts.update({page.number: page.text for page in document.page_texts})
        if not documents:
            return None
        from dataclasses import replace

        combined = replace(documents[0], text="\n\n".join(document.text for document in documents),
            pages_read=tuple(page_texts), page_texts=tuple(page for document in documents for page in document.page_texts),
            truncated=any(document.truncated for document in documents))
        return document_snapshot(combined)

    def _fail(self, area: str, action: str, error: object, url: str = "") -> None:
        if self.on_failure is None:
            return
        with contextlib.suppress(Exception):
            self.on_failure(area, action, error, url=url)


def _fetch_bytes(value: object) -> bytes:
    """Binary provider boundary: an integer must never allocate a fake PDF."""
    if value is None:
        return b""
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise ValueError("fetch bytes must be binary data")
    return bytes(value)


def _as_int(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value) if isinstance(value, (int, float, str)) else default
    except (TypeError, ValueError, OverflowError):
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
