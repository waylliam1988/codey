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
_SOURCE_ID_TOKEN_PATTERN = r"s\d+(?![A-Za-z0-9_/-])"
_SOURCE_ID_LABEL_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_!])"
    r"(?:来源|引用|source|reference|ref)"
    r"\s*[:：#=-]?\s*"
    rf"({_SOURCE_ID_TOKEN_PATTERN}(?:\s*(?:[,，、;/]|and|&)\s*{_SOURCE_ID_TOKEN_PATTERN})*)"
)
_SOURCE_ID_PAREN_GROUP_RE = re.compile(r"(?i)[\(（]([^\n()（）]{1,160})[\)）]")
_SOURCE_ID_TABLE_CELL_RE = re.compile(
    r"(?im)(?P<prefix>^|[|])\s*"
    rf"(?P<ids>{_SOURCE_ID_TOKEN_PATTERN}(?:\s*(?:[,，、;/]|and|&)\s*{_SOURCE_ID_TOKEN_PATTERN})*)"
    r"(?=\s*(?:[|]|[\(（]|[:：]))"
)
_SOURCE_ID_LINE_RE = re.compile(
    r"(?im)^[ \t]*(?:[-*+]\s+|\d+[.)、]\s+)?"
    r"(s\d+)(?=\s*(?:[:：]|[\(（]))"
)
_SOURCE_ID_TOKEN_RE = re.compile(rf"(?i){_SOURCE_ID_TOKEN_PATTERN}")
_SOURCE_ID_PAGE_SUFFIX_RE = re.compile(
    r"(?i)\s+(?:p\.?|pp\.?|pages?|page)\s*\.?\s*\d+(?:\s*-\s*\d+)?"
)
_SOURCE_ID_GROUP_ALLOWED_RE = re.compile(
    r"(?i)(?:来源|引用|source|reference|ref|and|p|pp|page|pages|s\d+|"
    r"[\d\s,，、;/&:.：#=\-]+)"
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
    refs.extend(_source_id_parenthetical_refs(value))
    for match in _SOURCE_ID_CONTEXT_RE.finditer(value):
        refs.append(SourceIdRef(
            source_id=match.group(1).lower(),
            start=match.start(),
            end=match.end(),
        ))
    for match in _SOURCE_ID_LABEL_RE.finditer(value):
        refs.extend(_source_id_group_refs(
            match.group(1),
            base_start=match.start(1),
            first_start=match.start(),
        ))
    for match in _SOURCE_ID_TABLE_CELL_RE.finditer(value):
        refs.extend(_source_id_group_refs(
            match.group("ids"),
            base_start=match.start("ids"),
        ))
    for match in _SOURCE_ID_LINE_RE.finditer(value):
        source_id_start = match.start(1)
        refs.append(SourceIdRef(
            source_id=match.group(1).lower(),
            start=source_id_start,
            end=match.end(1),
        ))
    return _dedupe_source_id_refs(refs)


def _source_id_parenthetical_refs(value: str) -> list[SourceIdRef]:
    refs: list[SourceIdRef] = []
    for match in _SOURCE_ID_PAREN_GROUP_RE.finditer(value):
        content = match.group(1)
        if not _is_source_id_group(content):
            continue
        refs.extend(_source_id_group_refs(
            content,
            base_start=match.start(1),
            first_start=match.start(),
            last_end=match.end(),
            bracketed=True,
        ))
    return refs


def _is_source_id_group(content: str) -> bool:
    if not _SOURCE_ID_TOKEN_RE.search(content):
        return False
    remainder = content
    for pattern in (
        r"(?i)来源|引用|source|reference|ref",
        r"(?i)s\d+",
        r"(?i)pages?|page|pp?\.?",
        r"\d+",
        r"[\s,，、;/&:.：#=\-]+",
    ):
        remainder = re.sub(pattern, "", remainder)
    if not remainder:
        return True
    return bool(_SOURCE_ID_GROUP_ALLOWED_RE.fullmatch(content))


def _source_id_group_refs(
    content: str,
    *,
    base_start: int,
    first_start: int | None = None,
    last_end: int | None = None,
    bracketed: bool = False,
) -> list[SourceIdRef]:
    token_matches = list(_SOURCE_ID_TOKEN_RE.finditer(content))
    refs: list[SourceIdRef] = []
    for index, token in enumerate(token_matches):
        start = base_start + token.start()
        end = base_start + token.end()
        if index == 0 and first_start is not None:
            start = first_start
        suffix = ""
        suffix_match = _SOURCE_ID_PAGE_SUFFIX_RE.match(content, token.end())
        if suffix_match:
            suffix = suffix_match.group(0)
            end = base_start + suffix_match.end()
        if index == len(token_matches) - 1 and last_end is not None:
            end = last_end
        refs.append(SourceIdRef(
            source_id=token.group(0).lower(),
            start=start,
            end=end,
            page_suffix=suffix,
            bracketed=bracketed,
        ))
    return refs


def _dedupe_source_id_refs(refs: list[SourceIdRef]) -> list[SourceIdRef]:
    ordered = sorted(refs, key=lambda item: (item.start, -item.end, not item.bracketed))
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
