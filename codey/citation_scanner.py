"""Shared citation and source-id scanners for bounded reports."""

from __future__ import annotations

import re
from dataclasses import dataclass


_CITATION_RE = re.compile(
    r"(?<![A-Za-z0-9_!])\[(\d+)(?:\s+(?:p\.?|pp\.?|pages?|page)\s*\.?\s*(\d+(?:\s*-\s*\d+)?))?\]",
    re.IGNORECASE,
)
_SOURCE_ID_BRACKET_RE = re.compile(
    r"(?<![A-Za-z0-9_!])\[\[?(s\d+)"
    r"((?:\s+(?:p\.?|pp\.?|pages?|page)\s*\.?\s*\d+(?:\s*-\s*\d+)?)?)\]\]?",
    re.IGNORECASE,
)
_SOURCE_ID_CONTEXT_RE = re.compile(
    r"(?i)(?:source[-_\s]*id|source[-_\s]*ref|citation[-_\s]*id|"
    r"internal[-_\s]*source|来源\s*id|引用\s*id)\s*[:=#-]?\s*(s\d+)(?![A-Za-z0-9_/-])"
)


@dataclass(frozen=True)
class CitationRef:
    number: int
    pages: tuple[int, ...] = ()


@dataclass(frozen=True)
class SourceIdRef:
    source_id: str
    start: int
    end: int
    page_suffix: str = ""
    bracketed: bool = False


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


def source_id_refs(text: str) -> set[str]:
    return {item.source_id for item in source_id_ref_items(text)}


def source_id_ref_items(text: str) -> list[SourceIdRef]:
    value = str(text or "")
    refs: list[SourceIdRef] = []
    for match in _SOURCE_ID_BRACKET_RE.finditer(value):
        refs.append(SourceIdRef(
            source_id=match.group(1).lower(),
            start=match.start(),
            end=match.end(),
            page_suffix=match.group(2) or "",
            bracketed=True,
        ))
    for match in _SOURCE_ID_CONTEXT_RE.finditer(value):
        refs.append(SourceIdRef(
            source_id=match.group(1).lower(),
            start=match.start(),
            end=match.end(),
        ))
    return _dedupe_source_id_refs(refs)


def source_id_bracket_ref_items(text: str) -> list[SourceIdRef]:
    return [item for item in source_id_ref_items(text) if item.bracketed]


def _dedupe_source_id_refs(refs: list[SourceIdRef]) -> list[SourceIdRef]:
    ordered = sorted(refs, key=lambda item: (item.start, item.end, not item.bracketed))
    kept: list[SourceIdRef] = []
    spans: set[tuple[int, int]] = set()
    for item in ordered:
        span = (item.start, item.end)
        if span in spans:
            continue
        if any(item.start < existing.end and existing.start < item.end for existing in kept):
            continue
        kept.append(item)
        spans.add(span)
    return kept


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
