"""Parse numbered source entries using the opened source metadata."""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from codey.research.ledger import ResearchLedger


_SOURCE_LINE_RE = re.compile(
    r"^\s*(?:[-*]\s*)?\[(\d+)\]\s*(.*?)\s*(?:-|–|—)\s*(https?://\S+)\s*$"
)
_SOURCE_MARKDOWN_LINK_RE = re.compile(
    r"^\s*(?:[-*]\s*)?\[(\d+)\]\s*\[(.*?)\]\((https?://\S+)\)\s*(?:[-–—].*)?$"
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
    quality: Mapping[str, object] = field(default_factory=dict[str, object])
    pages: tuple[int, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "number": self.number,
            "title": self.title,
            "url": self.url,
            "quality": dict(self.quality),
            "pages": list(self.pages),
        }


def parse_citation_rows(sources_section: str, ledger: ResearchLedger | None = None) -> list[Citation]:
    citations: list[Citation] = []
    for line in (sources_section or "").splitlines():
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
            url = _trim_source_url(url_first_match.group(2))
            raw_title = url_first_match.group(3) or ""
        else:
            assert match is not None
            number = int(match.group(1))
            url = _trim_source_url(match.group(3))
            raw_title = match.group(2)
        title = _citation_title(raw_title, url, ledger)
        quality = ledger.quality_for_url(url).to_dict() if ledger is not None else {}
        citations.append(Citation(number=number, title=title, url=url, quality=quality))
    return citations


def _trim_source_url(url: str) -> str:
    text = (url or "").strip().rstrip(".,;:，。；、]】`\"'")
    while text.endswith(")") and text.count(")") > text.count("("):
        text = text[:-1].rstrip(".,;:，。；、]】`\"'")
    return text


def parse_citations(sources_section: str, ledger: ResearchLedger | None = None) -> list[Citation]:
    citations: list[Citation] = []
    seen: set[int] = set()
    for item in parse_citation_rows(sources_section, ledger):
        if item.number in seen:
            continue
        seen.add(item.number)
        citations.append(item)
    return citations


def _citation_title(raw: str, url: str, ledger: ResearchLedger | None) -> str:
    title = re.sub(r"\bAvailable at:?\s*$", "", (raw or ""), flags=re.IGNORECASE)
    title = title.strip().strip("-–—:：.。")
    if title:
        return title
    if ledger is not None:
        opened_title = ledger.source_title(url).strip()
        if opened_title:
            return opened_title
    return "Source"
