"""Deterministic quality gate for Research final reports."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Mapping

from codey.research.citation_scanner import (
    CitationRef,
    citation_ref_items,
    citation_refs,
    source_id_ref_items,
    source_id_refs,
)
from codey.research.ledger import ResearchLedger
from codey.research.provenance import provenance_problem
from codey.report_sections import (
    heading_key as _heading_key,
    missing_required_sections as _missing_required_sections,
    parse_sections,
    section_title,
)

_SOURCE_LINE_RE = re.compile(
    r"^\s*(?:[-*]\s*)?\[(\d+)\]\s*(.*?)\s*(?:-|–|—)\s*(https?://\S+)\s*$"
)
_SOURCE_MARKDOWN_LINK_RE = re.compile(
    r"^\s*(?:[-*]\s*)?\[(\d+)\]\s*\[(.*?)\]\((https?://[^)\s]+)\)\s*(?:[-–—].*)?$"
)
_SOURCE_BRACKET_URL_RE = re.compile(
    r"^\s*(?:[-*]\s*)?\[(\d+)\]\s*(.*?)\s*(https?://\S+)\s*$"
)
_SOURCE_NUMBERED_URL_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(\d+)[\.)、]\s*(.*?)\s*(https?://\S+)\s*$"
)
_SOURCE_NUMBERED_URL_FIRST_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(\d+)[\.)、]\s*(https?://\S+)\s*(?:[-–—]\s*(.*?))?\s*$"
)


@dataclass(frozen=True)
class Citation:
    number: int
    title: str
    url: str
    quality: dict = field(default_factory=dict)
    pages: tuple[int, ...] = ()

    def to_dict(self) -> dict:
        return {
            "number": self.number,
            "title": self.title,
            "url": self.url,
            "quality": dict(self.quality),
            "pages": list(self.pages),
        }


@dataclass(frozen=True)
class ReportQualityReview:
    ok: bool
    message: str
    warnings: tuple[str, ...] = ()
    citation_map: tuple[Citation, ...] = ()
    counterpoints: tuple[str, ...] = ()
    sections: Mapping[str, str] = field(default_factory=dict)

    def citation_payload(self) -> list[dict]:
        return [item.to_dict() for item in self.citation_map]


def review_report_quality(
    summary: str,
    *,
    ledger: ResearchLedger,
    opened_sources: set[str],
    search_result_urls: set[str],
) -> ReportQualityReview:
    sections = parse_sections(summary)
    missing = _missing_required_sections(sections)
    if missing:
        return _missing_required_sections_review(missing)
    source_id_review = _source_id_leak_review(summary, sections, ledger)
    if source_id_review:
        return source_id_review
    no_citable_review = _no_citable_source_review(
        summary,
        sections,
        ledger,
        opened_sources=opened_sources,
        search_result_urls=search_result_urls,
    )
    if no_citable_review:
        return no_citable_review
    provenance_review = _provenance_review(
        sections,
        summary,
        opened_sources=opened_sources,
        search_result_urls=search_result_urls,
    )
    if provenance_review:
        return provenance_review
    if not ledger.final_url_set():
        return ReportQualityReview(
            False,
            "Report quality failed: no opened source is available for citation. "
            "Search and open a source before calling done.",
        )

    source_review, citations, source_numbers = _review_source_table(sections, ledger)
    if source_review:
        return source_review
    content_review = _required_body_citation_review(sections)
    if content_review:
        return content_review

    warnings = _source_quality_warnings(citations, source_numbers, sections)

    return ReportQualityReview(
        True,
        "report quality review passed",
        tuple(warnings),
        citation_map=citations,
        counterpoints=tuple(_section_lines(sections["counter"])),
        sections=sections,
    )


def _missing_required_sections_review(missing: list[str]) -> ReportQualityReview:
    return ReportQualityReview(
        False,
        "Report quality failed: missing required section(s): "
        + ", ".join(section_title(item) for item in missing)
        + ". Revise done.answer using the required Research report template.",
    )


def _source_id_leak_review(
    summary: str,
    sections: Mapping[str, str],
    ledger: ResearchLedger,
) -> ReportQualityReview | None:
    sources_text = sections.get("sources", "")
    source_id_values = source_id_refs(_text_without_sources(summary))
    source_id_values.update(_source_section_source_id_refs(sources_text, ledger))
    if source_id_values:
        return ReportQualityReview(
            False,
            "Report quality failed: source-id citation(s) must be compiled to numbered citations: "
            + ", ".join(f"[{item}]" for item in sorted(source_id_values)[:6]),
        )
    return None


def _no_citable_source_review(
    summary: str,
    sections: Mapping[str, str],
    ledger: ResearchLedger,
    *,
    opened_sources: set[str],
    search_result_urls: set[str],
) -> ReportQualityReview | None:
    if ledger.final_url_set() or not _is_no_citable_source_report(
        summary,
        sections,
        ledger,
        opened_sources=opened_sources,
        search_result_urls=search_result_urls,
    ):
        return None
    return ReportQualityReview(
        True,
        "report quality review passed: no citable opened source found",
        ("no opened source was available for citation",),
        sections=sections,
    )


def _provenance_review(
    sections: Mapping[str, str],
    summary: str,
    *,
    opened_sources: set[str],
    search_result_urls: set[str],
) -> ReportQualityReview | None:
    provenance = provenance_problem(
        _strict_provenance_text(sections, summary),
        opened_sources=opened_sources,
        search_result_urls=search_result_urls,
    )
    if provenance:
        return ReportQualityReview(False, provenance)
    context_provenance = provenance_problem(
        _context_provenance_text(sections),
        opened_sources=opened_sources,
        search_result_urls=search_result_urls,
        allow_search_result_mentions=True,
    )
    if context_provenance:
        return ReportQualityReview(False, context_provenance)
    return None


def _review_source_table(
    sections: Mapping[str, str],
    ledger: ResearchLedger,
) -> tuple[ReportQualityReview | None, tuple[Citation, ...], set[int]]:
    body_ref_items = citation_ref_items(_without_sources(sections))
    pages_by_number = _pages_by_number(body_ref_items)
    source_rows = parse_citation_rows(sections["sources"], ledger)
    duplicate_sources = _conflicting_source_numbers(source_rows, ledger)
    if duplicate_sources:
        return (
            ReportQualityReview(
                False,
                "Report quality failed: duplicate 来源 number(s) map to multiple URLs: "
                + ", ".join(f"[{item}]" for item in duplicate_sources[:6]),
            ),
            (),
            set(),
        )
    citations = tuple(
        replace(item, pages=pages_by_number.get(item.number, ()))
        for item in parse_citations(sections["sources"], ledger)
    )
    if not citations:
        return (
            ReportQualityReview(
                False,
                "Report quality failed: the 来源 section must list numbered sources like "
                "[1] Title - https://final-url.",
            ),
            (),
            set(),
        )
    source_numbers = {item.number for item in citations}
    body_refs = {item.number for item in body_ref_items}
    missing_sources = sorted(body_refs - source_numbers)
    if missing_sources:
        return (
            ReportQualityReview(
                False,
                "Report quality failed: citation number(s) appear in the report but not in 来源: "
                + ", ".join(f"[{item}]" for item in missing_sources[:6]),
            ),
            (),
            set(),
        )
    final_urls = ledger.final_url_set()
    unopened = [item.url for item in citations if item.url not in final_urls]
    if unopened:
        return (
            ReportQualityReview(
                False,
                "Report quality failed: 来源 URL(s) were not opened as final URLs in this run: "
                + ", ".join(unopened[:3]),
            ),
            (),
            set(),
        )
    citation_urls = {item.url for item in citations}
    evidence_urls = {
        str(item.get("source_url") or "")
        for item in ledger.evidence_payload()
        if str(item.get("source_url") or "") and str(item.get("excerpt") or "").strip()
    }
    missing_evidence = sorted(citation_urls - evidence_urls)
    if missing_evidence:
        return (
            ReportQualityReview(
                False,
                "Report quality failed: every cited source needs at least one saved evidence snippet "
                "copied from opened page text. Use knowledge_write with evidence.source_url and an exact "
                "evidence.excerpt before done. Missing snippet-backed citation(s): "
                + ", ".join(missing_evidence[:3]),
            ),
            (),
            set(),
        )
    page_problem = _page_citation_problem(list(citations), ledger)
    if page_problem:
        return (ReportQualityReview(False, page_problem), (), set())
    return None, citations, source_numbers


def _required_body_citation_review(sections: Mapping[str, str]) -> ReportQualityReview | None:
    if not citation_refs(sections["conclusion"]):
        return ReportQualityReview(
            False,
            "Report quality failed: the 结论 section must cite evidence with [n].",
        )
    if not citation_refs(sections["evidence"]):
        return ReportQualityReview(
            False,
            "Report quality failed: the 关键证据 section must cite opened sources with [n].",
        )
    counter_text = sections["counter"]
    if not citation_refs(counter_text) and not _says_no_strong_counter(counter_text):
        return ReportQualityReview(
            False,
            "Report quality failed: 反证与限制 must cite counter-evidence with [n], "
            "or explicitly say 未找到强反证 and explain what was searched.",
        )
    return None


def _source_quality_warnings(
    citations: tuple[Citation, ...],
    source_numbers: set[int],
    sections: Mapping[str, str],
) -> list[str]:
    warnings: list[str] = []
    if len(citations) < 2:
        warnings.append("strong conclusion is supported by only one cited source")
    quality_refs = citation_refs(sections["source_quality"])
    if not source_numbers.issubset(quality_refs):
        warnings.append("来源质量 does not assess every cited source")
    groups = [str(item.quality.get("independent_group") or "") for item in citations]
    if len(citations) > 1 and len({item for item in groups if item}) < len(citations):
        warnings.append("some cited sources may not be independent")
    if citations and all(str(item.quality.get("freshness") or "") == "undated" for item in citations):
        warnings.append("all cited sources look undated")
    return warnings


def parse_citation_rows(sources_section: str, ledger: ResearchLedger | None = None) -> list[Citation]:
    citations: list[Citation] = []
    for line in str(sources_section or "").splitlines():
        stripped = line.strip()
        match = (
            _SOURCE_MARKDOWN_LINK_RE.match(stripped)
            or _SOURCE_LINE_RE.match(stripped)
            or _SOURCE_BRACKET_URL_RE.match(stripped)
            or _SOURCE_NUMBERED_URL_RE.match(stripped)
        )
        url_first_match = None if match else _SOURCE_NUMBERED_URL_FIRST_RE.match(stripped)
        if not match and not url_first_match:
            continue
        if url_first_match:
            number = int(url_first_match.group(1))
            url = url_first_match.group(2).rstrip(".,;:，。；、)]")
            raw_title = url_first_match.group(3) or ""
        else:
            number = int(match.group(1))
            url = match.group(3).rstrip(".,;:，。；、)]")
            raw_title = match.group(2)
        title = _citation_title(raw_title, url, ledger)
        quality = ledger.quality_for_url(url).to_dict() if ledger is not None else {}
        citations.append(Citation(number=number, title=title, url=url, quality=quality))
    return citations


def parse_citations(sources_section: str, ledger: ResearchLedger | None = None) -> list[Citation]:
    citations: list[Citation] = []
    seen: set[int] = set()
    for item in parse_citation_rows(sources_section, ledger):
        if item.number in seen:
            continue
        seen.add(item.number)
        citations.append(item)
    return citations


def _pages_by_number(refs: list[CitationRef]) -> dict[int, tuple[int, ...]]:
    found: dict[int, list[int]] = {}
    for ref in refs:
        if not ref.pages:
            continue
        bucket = found.setdefault(ref.number, [])
        for page in ref.pages:
            if page not in bucket:
                bucket.append(page)
    return {number: tuple(pages) for number, pages in found.items()}


def _page_citation_problem(citations: list[Citation], ledger: ResearchLedger) -> str:
    for citation in citations:
        pages = set(citation.pages)
        if not pages:
            continue
        pages_read = ledger.pages_read_for_url(citation.url)
        unread = sorted(pages - pages_read)
        if unread:
            return (
                "Report quality failed: page citation(s) reference PDF page(s) not read from "
                f"[{citation.number}] {citation.url}: "
                + ", ".join(f"p.{page}" for page in unread[:6])
            )
        evidence_pages = ledger.evidence_pages_for_url(citation.url)
        if not evidence_pages.intersection(pages):
            return (
                "Report quality failed: page citation(s) need snippet-backed evidence from the cited page(s) "
                f"for [{citation.number}] {citation.url}: "
                + ", ".join(f"p.{page}" for page in sorted(pages)[:6])
            )
    return ""


def _without_sources(sections: Mapping[str, str]) -> str:
    return "\n\n".join(value for key, value in sections.items() if key != "sources")


def _text_without_sources(summary: str) -> str:
    lines: list[str] = []
    in_sources = False
    for line in str(summary or "").splitlines():
        key = _heading_key(line)
        if key == "sources":
            in_sources = True
            continue
        if key and in_sources:
            in_sources = False
        if not in_sources:
            lines.append(line)
    return "\n".join(lines)


def _source_section_source_id_refs(source_text: str, ledger: ResearchLedger) -> set[str]:
    refs: set[str] = set()
    for line in str(source_text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if parse_citation_rows(stripped, ledger):
            refs.update(
                item.source_id
                for item in source_id_ref_items(stripped)
                if not item.bracketed
            )
            continue
        refs.update(source_id_refs(stripped))
    return refs


def _conflicting_source_numbers(
    source_rows: list[Citation],
    ledger: ResearchLedger,
) -> tuple[int, ...]:
    urls_by_number: dict[int, set[str]] = {}
    for item in source_rows:
        canonical = ledger.canonical_opened_url(item.url) or item.url
        urls_by_number.setdefault(item.number, set()).add(canonical)
    return tuple(sorted(number for number, urls in urls_by_number.items() if len(urls) > 1))


def _citation_title(raw: str, url: str, ledger: ResearchLedger | None) -> str:
    title = re.sub(r"\bAvailable at:?\s*$", "", str(raw or ""), flags=re.IGNORECASE)
    title = title.strip().strip("-–—:：.。")
    if title:
        return title
    if ledger is not None:
        opened_title = ledger.source_title(url).strip()
        if opened_title:
            return opened_title
    return "Source"


def _strict_provenance_text(sections: Mapping[str, str], fallback: str) -> str:
    parts = [
        _preamble_text(fallback),
        sections.get("conclusion", ""),
        sections.get("evidence", ""),
        sections.get("source_quality", ""),
        sections.get("sources", ""),
    ]
    text = "\n\n".join(part for part in parts if part).strip()
    return text or fallback


def _preamble_text(summary: str) -> str:
    lines: list[str] = []
    for line in str(summary or "").splitlines():
        if _heading_key(line):
            break
        lines.append(line)
    return "\n".join(lines).strip()


def _context_provenance_text(sections: Mapping[str, str]) -> str:
    return "\n\n".join(
        part for part in (sections.get("counter", ""), sections.get("coverage", ""))
        if part
    )


def _is_no_citable_source_report(
    summary: str,
    sections: Mapping[str, str],
    ledger: ResearchLedger,
    *,
    opened_sources: set[str],
    search_result_urls: set[str],
) -> bool:
    if ledger.final_url_set() or not ledger.searches:
        return False
    if citation_ref_items(_without_sources(sections)):
        return False
    sources = sections.get("sources", "")
    if source_id_refs(sources):
        return False
    if parse_citations(sources, ledger) or "http://" in sources.lower() or "https://" in sources.lower():
        return False
    if provenance_problem(
        summary,
        opened_sources=opened_sources,
        search_result_urls=search_result_urls,
        allow_search_result_mentions=True,
    ):
        return False
    source_text = _normalized_body(sources)
    if not any(marker in source_text for marker in _NO_CITABLE_SOURCE_MARKERS):
        return False
    report_text = _normalized_body(
        "\n\n".join(
            sections.get(key, "")
            for key in ("conclusion", "evidence", "counter", "coverage")
        )
    )
    return any(marker in report_text for marker in _INSUFFICIENT_EVIDENCE_MARKERS)


_NO_CITABLE_SOURCE_MARKERS = (
    "无可引用",
    "无有效来源",
    "无可引用的有效来源",
    "no citable source",
    "no valid source",
    "no opened source",
    "no sources",
)

_INSUFFICIENT_EVIDENCE_MARKERS = (
    "未能确认",
    "无法确认",
    "无法验证",
    "未找到任何可验证",
    "未找到可验证",
    "insufficient evidence",
    "could not verify",
    "cannot verify",
    "unable to confirm",
    "not enough evidence",
    "no verifiable evidence",
)


def _normalized_body(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").lower())


def _says_no_strong_counter(text: str) -> bool:
    lower = str(text or "").lower()
    return "未找到强反证" in lower or "no strong counter" in lower or "no strong contrary" in lower


def _section_lines(text: str, limit: int = 6) -> list[str]:
    out: list[str] = []
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        stripped = stripped.removeprefix("- ").removeprefix("* ").strip()
        if stripped:
            out.append(stripped)
        if len(out) >= limit:
            break
    return out
