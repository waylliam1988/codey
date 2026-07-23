"""Canonical source document model for Research intake."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SourcePage:
    number: int
    text: str
    char_start: int = 0
    char_end: int = 0


@dataclass(frozen=True)
class SourceDocument:
    requested_url: str
    final_url: str
    title: str = ""
    content_kind: str = "html"
    mime_type: str = ""
    text: str = ""
    truncated: bool = False
    page_count: int = 0
    pages_read: tuple[int, ...] = ()
    page_texts: tuple[SourcePage, ...] = ()

    @classmethod
    def html(
        cls,
        *,
        requested_url: str,
        final_url: str,
        title: str,
        text: str,
        mime_type: str = "text/html",
        truncated: bool = False,
    ) -> "SourceDocument":
        return cls(
            requested_url=requested_url,
            final_url=final_url,
            title=title,
            content_kind="html",
            mime_type=mime_type,
            text=text,
            truncated=truncated,
        )


def compact_pages(values: object) -> str:
    if not isinstance(values, (list, tuple, set)):
        return ""
    pages: list[int] = []
    for value in values:
        try:
            page = int(value)
        except (TypeError, ValueError):
            continue
        if page > 0 and page not in pages:
            pages.append(page)
    pages.sort()
    if not pages:
        return ""
    ranges: list[str] = []
    start = prev = pages[0]
    for page in pages[1:]:
        if page == prev + 1:
            prev = page
            continue
        ranges.append(f"{start}-{prev}" if start != prev else str(start))
        start = prev = page
    ranges.append(f"{start}-{prev}" if start != prev else str(start))
    return ",".join(ranges)
