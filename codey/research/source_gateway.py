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
import hashlib
import re
from dataclasses import dataclass, field, replace
from typing import Protocol, runtime_checkable
from urllib.parse import quote

from codey.policies.network import check_fetch_url
from codey.research.ledger import ResearchLedger
from codey.research.pdf_extract import (
    PDF_DEFAULT_PAGES,
    PDF_MAX_PAGES_PER_OPEN,
    PdfSkipped,
    extract_pdf_document,
)
from codey.research.search_contracts import SearchOutcome, SearchState, failed_search
from codey.research.source_acquisition import acquisition_observation, combine_acquisitions, with_attempt
from codey.research.source_connectors import is_valid_arxiv_id
from codey.research.source_document import PublicationMetadata, SourceDocument, SourceReadScope
from codey.research.source_observation import source_snapshot
from codey.research.source_search import (
    SOURCE_SEARCH_DEFAULT_LIMIT,
    SourceSearchHit,
    bounded_limit,
    search_pages,
    search_text,
)
from codey.research.source_snapshots import captured_source, document_snapshot
from codey.research.url_selection import source_candidate_skip_reason
from codey.research.urls import parsed_url
from codey.runtime.core import cancellation
from codey.task.web_intent import source_url_key
from codey.types.json_value import JsonObject
from codey.types.source_failure import SourceFailureCode, source_failure_code

OPEN_DEFAULT_LIMIT = 6000
OPEN_MAX_LIMIT = 12000
OPEN_MIN_LIMIT = 500
SEARCH_LIMIT = 8
PDF_SOURCE_SEARCH_MAX_PAGES = 30


@dataclass(frozen=True)
class FullTextCandidate:
    url: str
    kind: str
    doi: str = ""


def _full_text_candidates(page: dict[str, object]) -> tuple[FullTextCandidate, ...]:
    if page.get("status") != "ok" or page.get("content_kind") != "abstract":
        return ()
    parsed = parsed_url(page.get("url"))
    results: list[FullTextCandidate] = []
    if parsed.scheme == "https" and parsed.netloc == "arxiv.org" and parsed.path.startswith("/abs/"):
        identity = parsed.path.removeprefix("/abs/")
        if is_valid_arxiv_id(identity):
            results.append(FullTextCandidate(f"https://arxiv.org/pdf/{identity}", "pdf"))
    publication = page.get("publication")
    if parsed.scheme == "https" and parsed.netloc == "pubmed.ncbi.nlm.nih.gov" and isinstance(publication, PublicationMetadata) and publication.pmcid:
        results.append(FullTextCandidate(f"https://pmc.ncbi.nlm.nih.gov/articles/{publication.pmcid}/", "html"))
    if isinstance(publication, PublicationMetadata) and publication.doi:
        results.append(FullTextCandidate("https://doi.org/" + quote(publication.doi, safe="/"), "html", publication.doi))
    return tuple(results)


def _identified_full_text(candidate: FullTextCandidate, fetched: dict[str, object], abstract: dict[str, object]) -> bool:
    final_url = fetched.get("url")
    if not isinstance(final_url, str) or fetched.get("content_kind", "html") != candidate.kind:
        return False
    if not candidate.doi:
        expected = {candidate.url, candidate.url + ".pdf"} if candidate.kind == "pdf" else {candidate.url, candidate.url.rstrip("/")}
        return final_url in expected
    if parsed_url(final_url).scheme != "https" or final_url == candidate.url:
        return False
    body = str(fetched.get("text") or "")
    if len(body) < max(900, len(str(abstract.get("text") or "")) * 2):
        return False
    doi = re.escape(candidate.doi)
    title = " ".join(str(abstract.get("title") or "").casefold().split())
    captured_title = " ".join(str(fetched.get("title") or "").casefold().split())
    headings = set(re.findall(r"(?im)^\s*(?:#{1,6}\s*)?(?:materials and )?(methods|results)\s*$", body))
    return (bool(title) and title in captured_title and {row.casefold() for row in headings} == {"methods", "results"}
            and re.search(r"(?:https?://(?:dx\.)?doi\.org/|(?<![\w/]))" + doi + r"(?![\w/-])", body[:4000], re.I) is not None)


@runtime_checkable
class _SearchProvider(Protocol):
    def search(self, query: str, *, limit: int) -> SearchOutcome: ...
    def fetch(self, url: str) -> dict[str, object]: ...


class SourceFailureRecorder(Protocol):
    def __call__(self, area: str, action: str, error: object, *, url: str = "") -> None: ...


@dataclass(frozen=True)
class SearchedSources:
    """Outcome of ``search``: raw provider hits, or an error string."""

    query: str
    hits: tuple[JsonObject, ...] = ()
    error: str = ""
    state: str = "unknown"


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
    acquisition: JsonObject = field(default_factory=dict)
    observation: JsonObject = field(default_factory=dict)
    failure_code: SourceFailureCode = "request_failed"


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
            outcome = self.search_provider.search(query, limit=limit)
        except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
            raise
        except Exception as exc:
            self._fail("search", "search", exc)
            detail = str(exc) or type(exc).__name__
            outcome = failed_search(query, type(self.search_provider).__name__, "search_execution_failed", detail)
        cancellation.check()
        results = outcome.rows
        self.ledger.record_search(query, results, attempts=outcome.attempts,
                                  state=outcome.state.value, failure_kind=outcome.failure_kind)
        if outcome.state == SearchState.FAILED:
            return SearchedSources(query=query, error=f"search failed: {outcome.failure_kind}: {outcome.detail}",
                                   state=outcome.state.value)
        return SearchedSources(query=query, hits=tuple(results), state=outcome.state.value)

    def open(
        self,
        url: str,
        *,
        offset: int = 0,
        limit: int = OPEN_DEFAULT_LIMIT,
        pages: str = "",
    ) -> OpenedSource:
        url = (url or "").strip()
        denied = self._admit_source_url(url)
        if denied is not None:
            return denied
        cancellation.check()
        try:
            page = self.search_provider.fetch(url)
        except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
            raise
        except Exception as exc:
            self._fail("browser", "open", exc, url)
            return OpenedSource(status="error", detail=f"open failed: {exc}",
                                failure_code="execution_failed")
        cancellation.check()
        candidates = _full_text_candidates(page)
        if candidates:
            return self._open_full_text(url, candidates, page, pages=pages)
        return self._receive_capture(url, page, pages=pages)

    def _open_full_text(self, url: str, candidates: tuple[FullTextCandidate, ...], abstract: dict[str, object],
                        *, pages: str) -> OpenedSource:
        original = with_attempt(abstract, "connector", url)
        attempts = [original]
        for candidate in candidates:
            acquired = self._fetch_full_text(candidate, abstract)
            attempts.append(acquired)
            preferred = self._receive_capture(url, combine_acquisitions(*attempts), pages=pages)
            if preferred.status == "ok":
                return preferred
            self._fail("browser", "open_full_text", preferred.detail, candidate.url)
        return self._receive_capture(url, {**original, "acquisition": combine_acquisitions(*attempts)["acquisition"]}, pages=pages)

    def _fetch_full_text(self, candidate: FullTextCandidate, abstract: dict[str, object]) -> dict[str, object]:
        reason = check_fetch_url(candidate.url, use_cache=True)
        if reason:
            return with_attempt({"status": "error", "failure_code": "policy_denied", "detail": reason,
                                 "delivery": "not_attempted"}, "browser", candidate.url)
        fetched: dict[str, object]
        try:
            fetched = self.search_provider.fetch(candidate.url)
        except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
            raise
        except Exception as exc:
            fetched = {"status": "error", "failure_code": "request_failed", "detail": str(exc) or type(exc).__name__}
        cancellation.check()
        if fetched.get("status") == "ok" and not _identified_full_text(candidate, fetched, abstract):
            final_url = fetched.get("url")
            failed: dict[str, object] = {"status": "error", "url": final_url if isinstance(final_url, str) else candidate.url,
                      "failure_code": "document_unreadable", "delivery": "confirmed",
                      "detail": "The candidate did not return the identified full-text article."}
            return with_attempt(failed, "browser", candidate.url)
        publication = abstract.get("publication")
        if candidate.doi and isinstance(publication, PublicationMetadata):
            publication = replace(publication, edition="", version="")
        return with_attempt({**fetched, "publication": publication}, "browser", candidate.url)

    def capture(self, url: str, page: dict[str, object], *, pages: str = "") -> OpenedSource:
        """Admit an already captured page through the ordinary source receiver."""
        url = url.strip()
        denied = self._admit_source_url(url)
        if denied is not None:
            return denied
        cancellation.check()
        return self._receive_capture(url, page, pages=pages)

    def _admit_source_url(self, url: str) -> OpenedSource | None:
        if not url:
            return OpenedSource(status="error", detail="open_url needs a url", failure_code="invalid_response")
        direct = source_url_key(url) in self.direct_urls
        skip_reason = "" if direct else source_candidate_skip_reason(url)
        if skip_reason:
            self._fail("browser", "open", skip_reason, url)
            return OpenedSource(
                status="skipped",
                detail=f"{skip_reason}. Choose a specific article, document, or readable source page.",
                failure_code="source_not_selected",
            )
        reason = check_fetch_url(url, use_cache=True)
        if reason:
            self._fail("browser", "open", reason, url)
            return OpenedSource(status="error", detail=reason, failure_code="policy_denied")
        return None

    def _receive_capture(self, url: str, page: dict[str, object], *, pages: str) -> OpenedSource:
        if page.get("observation") == "current_page":
            from codey.research.browser_page import validate_browser_capture

            page = validate_browser_capture(page)
        direct = source_url_key(url) in self.direct_urls
        acquisition = acquisition_observation(page["acquisition"]) if "acquisition" in page else {}
        page_url = str(page.get("url") or url)
        status = page.get("status")
        if type(status) is not str or status not in {"ok", "error", "skipped"}:
            return OpenedSource(status="error", detail="fetch returned an invalid structured status", acquisition=acquisition,
                                failure_code="invalid_response")
        skip_reason = "" if direct else source_candidate_skip_reason(page_url)
        if skip_reason:
            self._fail("browser", "open", skip_reason + " after redirect", url)
            return OpenedSource(
                status="skipped",
                detail=f"{skip_reason} after redirect. Choose a specific article, document, or readable source page.",
                acquisition=acquisition,
                failure_code="source_not_selected",
            )
        if status != "ok":
            message = page.get("detail")
            if type(message) is not str or not message:
                return OpenedSource(status="error", detail="fetch returned an invalid failure detail", acquisition=acquisition,
                                    failure_code="invalid_response")
            self._fail("browser", "open", message, url)
            return OpenedSource(status=status, detail=message, acquisition=acquisition,
                failure_code=source_failure_code(page.get("failure_code", "request_failed")))
        reason = check_fetch_url(page_url, use_cache=True)
        if reason:
            self._fail("browser", "open", f"{reason} (after redirect)", page_url or url)
            return OpenedSource(status="error", detail=f"{reason} (after redirect)", acquisition=acquisition,
                                failure_code="policy_denied")
        try:
            document = self._source_document_from_fetch(url, page, pages=pages)
        except (ValueError, TypeError) as exc:
            self._fail("browser", "open", exc, url)
            return OpenedSource(status="error", detail=f"invalid fetched document: {exc}", acquisition=acquisition,
                                failure_code="invalid_response")
        if isinstance(document, PdfSkipped):
            return OpenedSource(
                status="skipped",
                detail=f"{document.reason}. Choose an HTML source or another readable PDF.",
                acquisition=acquisition,
                failure_code="document_unreadable",
            )
        if not document.text.strip():
            return OpenedSource(status="error", detail="fetched document contains no readable text",
                                acquisition=acquisition, failure_code="page_not_loaded")
        read_urls = (url,) if not page_url or page_url == url else (url, page_url)
        source = self.ledger.record_open_document(document)
        assert source is not None
        observation = source_snapshot(source, document.text, {page.number: page.text for page in document.page_texts})
        return OpenedSource(status="ok", document=document, read_urls=read_urls, acquisition=acquisition,
                            observation=observation)

    def _source_document_from_fetch(
        self, requested_url: str, page: dict[str, object], *, pages: str = ""
    ) -> SourceDocument | PdfSkipped:
        final_url = str(page.get("url") or requested_url)
        content_kind = str(page.get("content_kind") or "").lower()
        mime_type = str(page.get("mime_type") or "")
        publication = page.get("publication")
        if content_kind == "pdf":
            pdf_bytes = _fetch_bytes(page.get("bytes"))
            document = extract_pdf_document(
                pdf_bytes,
                requested_url=requested_url,
                final_url=final_url,
                title=str(page.get("title") or ""),
                mime_type=mime_type or "application/pdf",
                pages=pages or PDF_DEFAULT_PAGES,
            )
            if isinstance(document, PdfSkipped):
                return document
            return replace(document, pdf_sha256=hashlib.sha256(pdf_bytes).hexdigest(),
                           publication=publication if isinstance(publication, PublicationMetadata) else None)
        text = str(page.get("text") or "")
        region = page.get("query_region")
        target = region.get("text") if isinstance(region, dict) else None
        return SourceDocument(
            requested_url=requested_url,
            final_url=final_url,
            title=str(page.get("title") or ""),
            content_kind="abstract" if content_kind == "abstract" else "html",
            text=text,
            mime_type=mime_type or "text/html",
            truncated=bool(page.get("truncated")),
            read_scope=SourceReadScope.locate(text, target) if isinstance(target, str) else None,
            publication=publication if isinstance(publication, PublicationMetadata) else None,
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
                    from codey.research.source_observation import source_observation

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
