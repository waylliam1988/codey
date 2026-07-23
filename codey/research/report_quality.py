"""Deterministic quality gate for Research final reports."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Mapping

from codey.research.ledger import ResearchLedger
from codey.research.provenance import provenance_problem

_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*$")
_CITATION_RE = re.compile(r"(?<![\w!])\[(\d+)\]")
_SOURCE_LINE_RE = re.compile(
    r"^\s*(?:[-*]\s*)?\[(\d+)\]\s*(.*?)\s*(?:-|–|—)\s*(https?://\S+)\s*$"
)
_SOURCE_MARKDOWN_LINK_RE = re.compile(
    r"^\s*(?:[-*]\s*)?\[(\d+)\]\s*\[(.*?)\]\((https?://[^)\s]+)\)\s*(?:[-–—].*)?$"
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

    def to_dict(self) -> dict:
        return {
            "number": self.number,
            "title": self.title,
            "url": self.url,
            "quality": dict(self.quality),
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
    provenance = provenance_problem(
        summary,
        opened_sources=opened_sources,
        search_result_urls=search_result_urls,
    )
    if provenance:
        return ReportQualityReview(False, provenance)
    sections = parse_sections(summary)
    missing = [label for label in REQUIRED_SECTIONS if not sections.get(label, "").strip()]
    if missing:
        return ReportQualityReview(
            False,
            "Report quality failed: missing required section(s): "
            + ", ".join(_section_title(item) for item in missing)
            + ". Revise done.answer using the required Research report template.",
        )
    if not ledger.final_url_set():
        return ReportQualityReview(
            False,
            "Report quality failed: no opened source is available for citation. "
            "Use web_search/open_url before calling done.",
        )

    citations = parse_citations(sections["sources"], ledger)
    if not citations:
        return ReportQualityReview(
            False,
            "Report quality failed: the 来源 section must list numbered sources like "
            "[1] Title - https://final-url.",
        )
    source_numbers = {item.number for item in citations}
    body_refs = citation_refs(_without_sources(sections))
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
        match = _SOURCE_MARKDOWN_LINK_RE.match(stripped) or _SOURCE_LINE_RE.match(stripped)
        if not match:
            continue
        number = int(match.group(1))
        if number in seen:
            continue
        seen.add(number)
        title = match.group(2).strip() or "Source"
        url = match.group(3).rstrip(".,;:，。；、)")
        quality = ledger.quality_for_url(url).to_dict() if ledger is not None else {}
        citations.append(Citation(number=number, title=title, url=url, quality=quality))
    return citations


def citation_refs(text: str) -> set[int]:
    out: set[int] = set()
    for value in _CITATION_RE.findall(str(text or "")):
        try:
            out.add(int(value))
        except ValueError:
            pass
    return out


def _without_sources(sections: Mapping[str, str]) -> str:
    return "\n\n".join(value for key, value in sections.items() if key != "sources")


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
