"""Bounded PDF text intake for Research open_url."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import re
from typing import Any

from codey.research.source_document import SourceDocument, SourcePage

PDF_MAX_BYTES = 20 * 1024 * 1024
PDF_DEFAULT_PAGES = "1-5"
PDF_MAX_PAGES_PER_OPEN = 10
PDF_MAX_TEXT_CHARS = 80_000
PDF_MAX_PAGE_STREAM_BYTES = 50 * 1024 * 1024
_SPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class PdfSkipped:
    reason: str


def parse_pages(
    value: str = "",
    *,
    default: str = PDF_DEFAULT_PAGES,
    max_pages: int = PDF_MAX_PAGES_PER_OPEN,
) -> tuple[int, ...]:
    text = str(value or default or "").strip().lower()
    text = text.replace("pages", "").replace("page", "")
    text = text.replace("pp.", "").replace("pp", "")
    text = text.replace("p.", "").replace("p", "")
    text = re.sub(r"(?<=\d)\s*-\s*(?=\d)", "-", text)
    numbers: list[int] = []
    for chunk in re.split(r"[,;\s]+", text):
        chunk = chunk.strip()
        if not chunk:
            continue
        match = re.fullmatch(r"(\d+)\s*-\s*(\d+)", chunk)
        if match:
            start = max(1, int(match.group(1)))
            end = max(1, int(match.group(2)))
            if end < start:
                start, end = end, start
            numbers.extend(range(start, end + 1))
            continue
        if chunk.isdigit():
            numbers.append(max(1, int(chunk)))
    if not numbers and value:
        return parse_pages("", default=default, max_pages=max_pages)
    out: list[int] = []
    for number in numbers:
        if number not in out:
            out.append(number)
        if len(out) >= max_pages:
            break
    return tuple(out)


def extract_pdf_document(
    pdf_bytes: bytes,
    *,
    requested_url: str,
    final_url: str,
    title: str = "",
    mime_type: str = "application/pdf",
    pages: str = "",
) -> SourceDocument | PdfSkipped:
    data = bytes(pdf_bytes or b"")
    if not data:
        return PdfSkipped("PDF response was empty")
    if len(data) > PDF_MAX_BYTES:
        return PdfSkipped(f"PDF is too large to read safely ({len(data)} bytes > {PDF_MAX_BYTES})")
    try:
        from pypdf import PdfReader
    except Exception as exc:
        return PdfSkipped(f"PDF text extraction is unavailable: {exc}")
    try:
        reader = PdfReader(BytesIO(data))
    except Exception as exc:
        return PdfSkipped(f"PDF text extraction failed: {exc}")
    try:
        page_count = len(reader.pages)
    except Exception as exc:
        return PdfSkipped(f"PDF page list is unreadable: {exc}")
    requested_pages = parse_pages(pages)
    selected = [number for number in requested_pages if 1 <= number <= page_count]
    if not selected:
        return PdfSkipped("requested PDF pages are outside the document range")

    page_texts: list[SourcePage] = []
    combined_parts: list[str] = []
    total_chars = 0
    truncated = False
    for number in selected[:PDF_MAX_PAGES_PER_OPEN]:
        try:
            page = reader.pages[number - 1]
        except Exception:
            continue
        if _page_stream_too_large(page):
            truncated = True
            continue
        try:
            extracted = page.extract_text() or ""
        except Exception:
            continue
        clean = _clean_pdf_text(extracted)
        if not clean:
            continue
        marker = f"[page {number}]\n"
        room = PDF_MAX_TEXT_CHARS - total_chars
        if room <= len(marker):
            truncated = True
            break
        body = clean[: room - len(marker)]
        if len(body) < len(clean):
            truncated = True
        char_start = total_chars + len(marker)
        char_end = char_start + len(body)
        combined_parts.append(marker + body)
        page_texts.append(SourcePage(number=number, text=body, char_start=char_start, char_end=char_end))
        total_chars += len(marker) + len(body) + 2
        if truncated:
            break
    if not page_texts:
        return PdfSkipped("PDF has no extractable text in the requested pages")
    text = "\n\n".join(combined_parts).strip()
    return SourceDocument(
        requested_url=requested_url,
        final_url=final_url or requested_url,
        title=title or _title_from_url(final_url or requested_url),
        content_kind="pdf",
        mime_type=mime_type or "application/pdf",
        text=text,
        truncated=truncated or len(selected) < len(requested_pages),
        page_count=page_count,
        pages_read=tuple(page.number for page in page_texts),
        page_texts=tuple(page_texts),
    )


def _page_stream_too_large(page: Any) -> bool:
    try:
        contents = page.get_contents()
    except Exception:
        return False
    if contents is None:
        return False
    if not isinstance(contents, (list, tuple)):
        contents = [contents]
    total = 0
    for item in contents:
        try:
            data = item.get_data()
        except Exception:
            continue
        try:
            total += len(data)
        except TypeError:
            continue
        if total > PDF_MAX_PAGE_STREAM_BYTES:
            return True
    return False


def _clean_pdf_text(value: str) -> str:
    lines = []
    for line in str(value or "").splitlines():
        cleaned = _SPACE_RE.sub(" ", line).strip()
        if cleaned:
            lines.append(cleaned)
    return "\n".join(lines).strip()


def _title_from_url(url: str) -> str:
    tail = str(url or "").rstrip("/").rsplit("/", 1)[-1]
    return tail or "PDF source"
