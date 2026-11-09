"""Versioned source captures reconstructed from the original read/search receipts."""
from __future__ import annotations

import hashlib
from dataclasses import replace

from codey.research.ledger import ResearchLedger
from codey.research.ledger_receipts import ledger_observation, source_observation, source_snapshot
from codey.research.pdf_extract import parse_pages
from codey.research.source_document import SourceDocument, SourcePage
from codey.types.json_value import JsonObject


def captured_source(ledger: ResearchLedger, url: str, text_hash: str = "") -> JsonObject:
    """Resolve captured bytes; a missing version never falls back to the live URL."""
    current = ledger.source_record_for_url(url)
    if current is not None and (not text_hash or current.text_hash == text_hash):
        return ledger_observation(ledger, "open", url=url)
    for passage in ledger.read_passages.values():
        if passage.source.final_url == url and passage.source.text_hash == text_hash:
            return source_snapshot(passage.source, passage.source_text, dict(passage.pages))
    for search in reversed(ledger.source_searches):
        snapshot = search["source_snapshot"]
        source, _text, _pages = source_observation(snapshot)
        if source.final_url == url and source.text_hash == text_hash:
            return snapshot
    raise ValueError("the captured source version is unavailable; open_url explicitly to acquire a new version")


def snapshot_document(observation: JsonObject, *, pages: str = "") -> SourceDocument:
    source, text, page_map = source_observation(observation)
    selected = parse_pages(pages) if pages else source.pages_read
    if pages:
        if source.content_kind != "pdf" or any(number not in page_map for number in selected):
            raise ValueError("requested pages are not in the captured source; use open_url to acquire them")
        text = "\n\n".join(f"[page {number}]\n{page_map[number]}" for number in selected)
        page_map = {number: page_map[number] for number in selected}
    return SourceDocument(requested_url=source.requested_url, final_url=source.final_url,
        title=source.title, text=text, content_kind=source.content_kind, mime_type=source.mime_type,
        truncated=source.truncated, page_count=source.page_count, pages_read=selected,
        page_texts=tuple(SourcePage(number=number, text=body) for number, body in page_map.items()))


def document_snapshot(document: SourceDocument) -> JsonObject:
    ledger = ResearchLedger()
    source = ledger.record_open_document(document)
    assert source is not None
    return source_snapshot(source, document.text, {page.number: page.text for page in document.page_texts})


def select_source_pages(observation: JsonObject, pages: str) -> JsonObject:
    """Select captured PDF pages while preserving acquisition time and metadata."""
    source, _text, _page_map = source_observation(observation)
    document = snapshot_document(observation, pages=pages)
    selected = replace(source, text_hash=hashlib.sha256(document.text.encode("utf-8", errors="replace")).hexdigest()[:16],
                       pages_read=document.pages_read)
    return source_snapshot(selected, document.text, {page.number: page.text for page in document.page_texts})
