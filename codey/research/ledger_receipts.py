"""Lossless Research observations inside the existing tool-result receipts.

No log or executor: encode the affected ledger records and project them on
restart. Source bodies are carried by the managed result receipt, never by
UI ledger payloads (which deliberately clip excerpts and counts).
"""
from __future__ import annotations

import copy
import hashlib
from dataclasses import asdict

from codey.research.ledger import (
    EvidenceItem,
    OpenedSource,
    ResearchLedger,
    SearchRecord,
    SearchResultRecord,
    SourceQuality,
)


def ledger_observation(ledger: ResearchLedger, kind: str, *, url: str = "", evidence_start: int = 0) -> dict:
    if kind == "web_search" and ledger.searches:
        return {"search": asdict(ledger.searches[-1])}
    if kind == "source_search" and ledger.source_searches:
        return {"source_search": copy.deepcopy(ledger.source_searches[-1])}
    if kind == "knowledge_write":
        return {"evidence": [asdict(item) for item in ledger.evidence_items[evidence_start:]]}
    source = ledger.source_record_for_url(url) if url else None
    if source is not None:
        return {"source": asdict(source), "text": ledger.source_text_for_url(url),
                "pages": [{"number": number, "text": text}
                          for number, text in ledger.source_pages_for_url(url).items()]}
    return {}


def restore_ledger_observation(ledger: ResearchLedger, observation: dict) -> None:
    """Apply one verified receipt, preserving source identity and timestamps.

    The entry stages the whole replay on a clone and commits only after all
    receipts pass. Malformed source bodies never become read evidence.
    """
    if not isinstance(observation, dict):
        raise ValueError("research observation must be an object")
    keys = set(observation)
    if keys == {"search"}:
        row = dict(observation["search"])
        row["results"] = tuple(SearchResultRecord(**item) for item in row["results"])
        ledger.searches.append(SearchRecord(**row))
    elif keys == {"source_search"}:
        row = observation["source_search"]
        if not isinstance(row, dict) or set(row) != {"source_url", "query", "hits"}:
            raise ValueError("invalid source search receipt")
        ledger.source_searches.append(copy.deepcopy(row))
    elif keys == {"source", "text", "pages"}:
        _restore_source(ledger, observation)
    elif keys == {"evidence"}:
        items = [EvidenceItem(**item) for item in observation["evidence"]]
        for item in items:
            if (not item.note_id or not ledger.canonical_opened_url(item.source_url)
                    or not ledger.excerpt_in_source(item.source_url, item.excerpt, page=item.page)):
                raise ValueError("recovered evidence has no opened source or archived note")
        ledger.add_evidence_items(items)
    elif keys:
        raise ValueError("invalid research observation fields")


def _restore_source(ledger: ResearchLedger, observation: dict) -> None:
    row = dict(observation["source"])
    row["quality"] = SourceQuality(**row["quality"])
    pages_read = row["pages_read"]
    if not isinstance(pages_read, (list, tuple)) or any(type(p) is not int or p < 1 for p in pages_read):
        raise ValueError("invalid recovered source pages")
    row["pages_read"] = tuple(pages_read)
    source = OpenedSource(**row)
    text = observation["text"]
    if (type(text) is not str or not source.final_url or not source.requested_url
            or type(source.truncated) is not bool or type(source.page_count) is not int
            or source.page_count < 0
            or hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16] != source.text_hash):
        raise ValueError("recovered source body differs from observation")
    page_map = {}
    for page in observation["pages"]:
        if (not isinstance(page, dict) or set(page) != {"number", "text"}
                or type(page["number"]) is not int or page["number"] < 1
                or type(page["text"]) is not str or page["number"] in page_map):
            raise ValueError("invalid recovered page text")
        page_map[page["number"]] = page["text"]
    ledger.opened_sources = [item for item in ledger.opened_sources
                             if not {item.final_url, item.requested_url} & {source.final_url, source.requested_url}]
    ledger.opened_sources.append(source)
    for url in {source.final_url, source.requested_url}:
        ledger._source_texts[url] = text
        ledger._source_pages[url] = dict(page_map)
