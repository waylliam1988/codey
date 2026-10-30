"""Lossless Research observations inside the existing tool-result receipts.

No log or executor: encode the affected ledger records and project them on
restart. Source bodies are carried by the managed result receipt, never by
UI ledger payloads (which deliberately clip excerpts and counts).
"""
from __future__ import annotations

import copy
import hashlib
from dataclasses import asdict, fields
from datetime import datetime
from typing import Any

from codey.research.ledger import (
    EvidenceItem,
    OpenedSource,
    ResearchLedger,
    SearchRecord,
    SearchResultRecord,
    SourceQuality,
)
from codey.research.urls import parsed_url


def _record_fields(value: object, model: type[Any]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {item.name for item in fields(model)}:
        raise ValueError(f"invalid {model.__name__} observation fields")
    return dict(value)


def _source_url(value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError("invalid observation URL")
    parsed = parsed_url(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("invalid observation URL")
    return value


def _timestamp(value: object) -> None:
    if type(value) is not str:
        raise ValueError("invalid observation timestamp")
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("invalid observation timestamp") from exc
    if stamp.tzinfo is None:
        raise ValueError("observation timestamp requires a timezone")


def search_observation(observation: object) -> SearchRecord:
    """Validate the exact search receipt for both live facts and recovery."""
    if not isinstance(observation, dict) or set(observation) != {"search"}:
        raise ValueError("missing or invalid search observation")
    row = _record_fields(observation["search"], SearchRecord)
    if type(row["query"]) is not str or not row["query"].strip():
        raise ValueError("invalid search observation query")
    _timestamp(row["timestamp"])
    if not isinstance(row["results"], (list, tuple)):
        raise ValueError("invalid search observation results")
    results = []
    for item in row["results"]:
        hit = _record_fields(item, SearchResultRecord)
        _source_url(hit["url"])
        if (type(hit["rank"]) is not int or hit["rank"] < 1
                or type(hit["title"]) is not str or type(hit["snippet"]) is not str):
            raise ValueError("invalid search result observation")
        results.append(SearchResultRecord(**hit))
    return SearchRecord(query=row["query"], timestamp=row["timestamp"], results=tuple(results))


def source_observation(observation: object) -> tuple[OpenedSource, str, dict[int, str]]:
    """Decode one source observation without mutating any task or ledger."""
    if not isinstance(observation, dict) or set(observation) != {"source", "text", "pages"}:
        raise ValueError("missing or invalid source observation")
    row = _record_fields(observation["source"], OpenedSource)
    _source_url(row["requested_url"])
    _source_url(row["final_url"])
    _timestamp(row["retrieved_at"])
    quality = _record_fields(row["quality"], SourceQuality)
    if any(type(value) is not str for value in quality.values()):
        raise ValueError("invalid source quality observation")
    row["quality"] = SourceQuality(**quality)
    pages_read = row["pages_read"]
    if (not isinstance(pages_read, (list, tuple))
            or any(type(page) is not int or page < 1 for page in pages_read)
            or len(set(pages_read)) != len(pages_read)):
        raise ValueError("invalid source observation pages")
    row["pages_read"] = tuple(pages_read)
    if (type(row["truncated"]) is not bool or type(row["page_count"]) is not int or row["page_count"] < 0
            or any(type(row[key]) is not str for key in ("title", "content_kind", "mime_type", "text_hash"))):
        raise ValueError("invalid source observation metadata")
    source = OpenedSource(**row)
    text = observation["text"]
    if (type(text) is not str or not text.strip()
            or hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16] != source.text_hash):
        raise ValueError("source body differs from observation")
    if not isinstance(observation["pages"], (list, tuple)):
        raise ValueError("invalid source page observations")
    page_map: dict[int, str] = {}
    for page in observation["pages"]:
        if (not isinstance(page, dict) or set(page) != {"number", "text"}
                or type(page["number"]) is not int or page["number"] < 1
                or type(page["text"]) is not str or page["number"] in page_map):
            raise ValueError("invalid source page observation")
        page_map[page["number"]] = page["text"]
    if (any(page not in page_map for page in source.pages_read)
            or source.page_count and any(page > source.page_count for page in page_map)):
        raise ValueError("source page observation outside read range")
    return source, text, page_map


def source_search_observation(observation: object) -> dict[str, Any]:
    """Validate locator previews without interpreting their display text."""
    if not isinstance(observation, dict) or set(observation) != {"source_search"}:
        raise ValueError("missing or invalid source search observation")
    row = observation["source_search"]
    if not isinstance(row, dict) or set(row) != {"source_url", "query", "hits"}:
        raise ValueError("invalid source search observation fields")
    _source_url(row["source_url"])
    if type(row["query"]) is not str or not row["query"].strip() or not isinstance(row["hits"], (list, tuple)):
        raise ValueError("invalid source search observation query or hits")
    for hit in row["hits"]:
        if (not isinstance(hit, dict) or set(hit) != {"offset", "page", "snippet"}
                or type(hit["offset"]) is not int or hit["offset"] < 0
                or type(hit["snippet"]) is not str
                or hit["page"] is not None and (type(hit["page"]) is not int or hit["page"] < 1)):
            raise ValueError("invalid source search locator observation")
    return copy.deepcopy(row)


def ledger_observation(ledger: ResearchLedger, kind: str, *, url: str = "", evidence_start: int = 0) -> dict[str, Any]:
    observation: dict[str, Any]
    if kind == "web_search" and ledger.searches:
        observation = {"search": asdict(ledger.searches[-1])}
        search_observation(observation)
        return observation
    if kind == "source_search" and ledger.source_searches:
        observation = {"source_search": copy.deepcopy(ledger.source_searches[-1])}
        source_search_observation(observation)
        return observation
    if kind in {"knowledge_write", "save_finding"}:
        return {"evidence": [asdict(item) for item in ledger.evidence_items[evidence_start:]]}
    source = ledger.source_record_for_url(url) if url else None
    if source is not None:
        observation = {"source": asdict(source), "text": ledger.source_text_for_url(url),
                       "pages": [{"number": number, "text": text}
                                 for number, text in ledger.source_pages_for_url(url).items()]}
        source_observation(observation)
        return observation
    raise ValueError(f"missing research observation for {kind}")


def restore_ledger_observation(ledger: ResearchLedger, observation: dict[str, Any]) -> None:
    """Apply one verified receipt, preserving source identity and timestamps.

    The entry stages the whole replay on a clone and commits only after all
    receipts pass. Malformed source bodies never become read evidence.
    """
    if not isinstance(observation, dict):
        raise ValueError("research observation must be an object")
    keys = set(observation)
    if keys == {"questions"}:
        from codey.research.questions import question_plan

        ledger.questions = question_plan(observation["questions"], ledger.questions)
    elif keys == {"search"}:
        ledger.searches.append(search_observation(observation))
    elif keys == {"source_search"}:
        ledger.source_searches.append(source_search_observation(observation))
    elif keys == {"source", "text", "pages"}:
        _restore_source(ledger, observation)
    elif keys == {"evidence"}:
        items = [EvidenceItem(**item) for item in observation["evidence"]]
        for item in items:
            from codey.research.passages import passage_evidence_valid

            if (not item.note_id or not ledger.canonical_opened_url(item.source_url)
                    or not (passage_evidence_valid(ledger, item.source_url, item.excerpt, item.locator)
                            if item.locator.startswith("passage:") else
                            ledger.excerpt_in_source(item.source_url, item.excerpt, page=item.page))):
                raise ValueError("recovered evidence has no opened source or archived note")
        ledger.add_evidence_items(items)
    else:
        raise ValueError("invalid research observation fields")


def _restore_source(ledger: ResearchLedger, observation: dict[str, Any]) -> None:
    source, text, page_map = source_observation(observation)
    ledger.merge_opened_source(source, text, page_map)
