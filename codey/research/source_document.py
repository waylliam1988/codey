"""Canonical source document model for Research intake."""

from __future__ import annotations

import re
from dataclasses import dataclass

from codey.types.json_value import JsonObject, decode_json_object


def normalized_doi(value: object) -> str:
    return value.casefold() if isinstance(value, str) and re.fullmatch(r"10\.\d{4,9}/[!-~]{1,200}", value) else ""


def normalized_pmcid(value: object) -> str:
    return value.upper() if isinstance(value, str) and re.fullmatch(r"PMC[1-9][0-9]{0,11}", value, re.I) else ""


@dataclass(frozen=True)
class PublicationMetadata:
    doi: str = ""
    pmcid: str = ""
    status: str = ""
    edition: str = ""
    version: str = ""

    def to_payload(self) -> JsonObject:
        return {"doi": self.doi, "pmcid": self.pmcid, "status": self.status,
                **({"edition": self.edition} if self.edition else {}),
                **({"version": self.version} if self.version else {})}

    @classmethod
    def decode(cls, value: object) -> PublicationMetadata:
        row = decode_json_object(value)
        if not {"doi", "pmcid", "status"} <= set(row) <= {"doi", "pmcid", "status", "edition", "version"}:
            raise ValueError("invalid publication metadata fields")
        doi, pmcid, status = row["doi"], row["pmcid"], row["status"]
        edition, version = row.get("edition", ""), row.get("version", "")
        if (not isinstance(doi, str) or doi != normalized_doi(doi) or not isinstance(pmcid, str)
                or pmcid != normalized_pmcid(pmcid)
                or not isinstance(status, str) or status not in {"", "retracted", "retraction_notice"}
                or not isinstance(edition, str) or edition not in {"", "preprint", "accepted", "published"}
                or not isinstance(version, str) or re.fullmatch(r"[a-zA-Z0-9._-]{0,40}", version) is None):
            raise ValueError("invalid publication metadata")
        return cls(doi, pmcid, status, edition, version)


@dataclass(frozen=True)
class SourceReadScope:
    """An observed target mapped onto the unchanged captured source text."""

    text: str
    start: int | None = None
    end: int | None = None

    @classmethod
    def locate(cls, text: str, target: str) -> SourceReadScope:
        normalized = " ".join(target.split())
        if not normalized:
            raise ValueError("source read target must contain text")
        pattern = re.compile(r"\s+".join(re.escape(part) for part in normalized.split(" ")))
        match = pattern.search(text)
        if match is None or pattern.search(text, match.start() + 1) is not None:
            return cls(normalized)
        return cls(normalized, match.start(), match.end())

    def bounds(self, text: str) -> tuple[int, int]:
        return (self.start, self.end) if self.start is not None and self.end is not None else (0, len(text))


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
    read_scope: SourceReadScope | None = None
    publication: PublicationMetadata | None = None
    pdf_sha256: str = ""

    @property
    def read_bounds(self) -> tuple[int, int]:
        return self.read_scope.bounds(self.text) if self.read_scope is not None else (0, len(self.text))

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
        read_scope: SourceReadScope | None = None,
    ) -> SourceDocument:
        return cls(
            requested_url=requested_url,
            final_url=final_url,
            title=title,
            content_kind="html",
            mime_type=mime_type,
            text=text,
            truncated=truncated,
            read_scope=read_scope,
        )


def compact_pages(values: object) -> str:
    if not isinstance(values, (list, tuple, set)):
        return ""
    pages: list[int] = []
    for value in values:
        if isinstance(value, bool):
            continue
        if isinstance(value, str) and not value.strip().isascii():
            continue
        try:
            page = int(value)
        except (TypeError, ValueError, OverflowError):
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
