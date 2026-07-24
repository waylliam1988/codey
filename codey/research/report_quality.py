"""Deterministic quality gate for Research final reports."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Mapping

from codey.research.ledger import ResearchLedger
from codey.research.provenance import provenance_problem

_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*$")
_CITATION_RE = re.compile(
    r"(?<![\w!])\[(\d+)(?:\s+(?:p\.?|pp\.?|pages?|page)\s*\.?\s*(\d+(?:\s*-\s*\d+)?))?\]",
    re.IGNORECASE,
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
_HEADING_NUMBER_RE = re.compile(
    r"^(?:"
    r"\d+(?:\.\d+)*"
    r"|[一二三四五六七八九十百千万]+"
    r"|[ivxlcdm]+"
    r")\s*[\.\)、)）:：、-]\s*",
    re.IGNORECASE,
)

SECTION_ALIASES: dict[str, tuple[str, ...]] = {
    "conclusion": ("结论", "关键结论", "conclusion", "key conclusions"),
    "evidence": ("关键证据", "evidence", "key evidence"),
    "counter": ("反证与限制", "反证", "限制", "counter-evidence", "counter", "limitations"),
    "source_quality": ("来源质量", "source quality", "source assessment"),
    "coverage": ("搜索覆盖", "research coverage", "search coverage", "coverage"),
    "sources": ("来源", "sources", "references"),
}
REQUIRED_SECTIONS = ("conclusion", "evidence", "counter", "source_quality", "coverage", "sources")


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
class CitationRef:
    number: int
    pages: tuple[int, ...] = ()


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
    missing = [label for label in REQUIRED_SECTIONS if not sections.get(label, "").strip()]
    if missing:
        return ReportQualityReview(
            False,
            "Report quality failed: missing required section(s): "
            + ", ".join(_section_title(item) for item in missing)
            + ". Revise done.answer using the required Research report template.",
        )
    if not ledger.final_url_set() and _is_no_citable_source_report(
        summary,
        sections,
        ledger,
        opened_sources=opened_sources,
        search_result_urls=search_result_urls,
    ):
        return ReportQualityReview(
            True,
            "report quality review passed: no citable opened source found",
            ("no opened source was available for citation",),
            sections=sections,
        )
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
    if not ledger.final_url_set():
        return ReportQualityReview(
            False,
            "Report quality failed: no opened source is available for citation. "
            "Use web_search/open_url before calling done.",
        )

    body_ref_items = citation_ref_items(_without_sources(sections))
    pages_by_number = _pages_by_number(body_ref_items)
    citations = [
        replace(item, pages=pages_by_number.get(item.number, ()))
        for item in parse_citations(sections["sources"], ledger)
    ]
    if not citations:
        return ReportQualityReview(
            False,
            "Report quality failed: the 来源 section must list numbered sources like "
            "[1] Title - https://final-url.",
        )
    source_numbers = {item.number for item in citations}
    body_refs = {item.number for item in body_ref_items}
    missing_sources = sorted(body_refs - source_numbers)
    if missing_sources:
        return ReportQualityReview(
            False,
            "Report quality failed: citation number(s) appear in the report but not in 来源: "
            + ", ".join(f"[{item}]" for item in missing_sources[:6]),
        )
    final_urls = ledger.final_url_set()
    unopened = [item.url for item in citations if item.url not in final_urls]
    if unopened:
        return ReportQualityReview(
            False,
            "Report quality failed: 来源 URL(s) were not opened as final URLs in this run: "
            + ", ".join(unopened[:3]),
        )
    citation_urls = {item.url for item in citations}
    evidence_urls = {
        str(item.get("source_url") or "")
        for item in ledger.evidence_payload()
        if str(item.get("source_url") or "") and str(item.get("excerpt") or "").strip()
    }
    missing_evidence = sorted(citation_urls - evidence_urls)
    if missing_evidence:
        return ReportQualityReview(
            False,
            "Report quality failed: every cited source needs at least one saved evidence snippet "
            "copied from opened page text. Use knowledge_write with evidence.source_url and an exact "
            "evidence.excerpt before done. Missing snippet-backed citation(s): "
            + ", ".join(missing_evidence[:3]),
        )
    page_problem = _page_citation_problem(citations, ledger)
    if page_problem:
        return ReportQualityReview(False, page_problem)
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

    return ReportQualityReview(
        True,
        "report quality review passed",
        tuple(warnings),
        citation_map=tuple(citations),
        counterpoints=tuple(_section_lines(counter_text)),
        sections=sections,
    )


def parse_sections(text: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current = ""
    for line in str(text or "").splitlines():
        key = _heading_key(line)
        if key:
            current = key
            sections.setdefault(current, [])
            continue
        if current:
            sections.setdefault(current, []).append(line)
    return {key: "\n".join(value).strip() for key, value in sections.items()}


def parse_citations(sources_section: str, ledger: ResearchLedger | None = None) -> list[Citation]:
    citations: list[Citation] = []
    seen: set[int] = set()
    for line in str(sources_section or "").splitlines():
        stripped = line.strip()
        match = (
            _SOURCE_MARKDOWN_LINK_RE.match(stripped)
            or _SOURCE_LINE_RE.match(stripped)
            or _SOURCE_BRACKET_URL_RE.match(stripped)
            or _SOURCE_NUMBERED_URL_RE.match(stripped)
        )
        if not match:
            continue
        number = int(match.group(1))
        if number in seen:
            continue
        seen.add(number)
        url = match.group(3).rstrip(".,;:，。；、)]")
        title = _citation_title(match.group(2), url, ledger)
        quality = ledger.quality_for_url(url).to_dict() if ledger is not None else {}
        citations.append(Citation(number=number, title=title, url=url, quality=quality))
    return citations


def citation_refs(text: str) -> set[int]:
    return {item.number for item in citation_ref_items(text)}


def citation_ref_items(text: str) -> list[CitationRef]:
    refs: list[CitationRef] = []
    for value, pages in _CITATION_RE.findall(str(text or "")):
        try:
            number = int(value)
        except ValueError:
            continue
        refs.append(CitationRef(number=number, pages=_parse_page_ref(pages)))
    return refs


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


def _parse_page_ref(value: str) -> tuple[int, ...]:
    text = str(value or "").strip()
    if not text:
        return ()
    match = re.fullmatch(r"(\d+)(?:\s*-\s*(\d+))?", text)
    if not match:
        return ()
    start = max(1, int(match.group(1)))
    end = max(1, int(match.group(2) or start))
    if end < start:
        start, end = end, start
    if end - start > 99:
        end = start + 99
    return tuple(range(start, end + 1))


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
        sections.get("conclusion", ""),
        sections.get("evidence", ""),
        sections.get("source_quality", ""),
        sections.get("sources", ""),
    ]
    text = "\n\n".join(part for part in parts if part).strip()
    return text or fallback


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


def _heading_key(line: str) -> str:
    stripped = str(line or "").strip()
    match = _HEADING_RE.match(stripped)
    if match:
        title = match.group(1)
    else:
        title = stripped.rstrip(":：")
    title = _normalize_heading(title)
    if not title:
        return ""
    for key in ("source_quality", "conclusion", "evidence", "counter", "coverage", "sources"):
        if title in {_normalize_heading(item) for item in SECTION_ALIASES[key]}:
            return key
    return ""


def _normalize_heading(value: str) -> str:
    text = str(value or "").strip().strip("#").strip()
    text = text.strip("*_`[]() ")
    while True:
        next_text = _HEADING_NUMBER_RE.sub("", text).strip()
        if next_text == text:
            break
        text = next_text
    text = text.rstrip(":：").strip()
    return re.sub(r"\s+", " ", text).lower()


def _section_title(key: str) -> str:
    return {
        "conclusion": "结论",
        "evidence": "关键证据",
        "counter": "反证与限制",
        "source_quality": "来源质量",
        "coverage": "搜索覆盖",
        "sources": "来源",
    }.get(key, key)


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
