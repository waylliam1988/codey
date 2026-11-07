"""Lossless Research observations inside the existing tool-result receipts.

No log or executor: encode the affected ledger records and project them on
restart. Source bodies are carried by the managed result receipt, never by
UI ledger payloads (which deliberately clip excerpts and counts).
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict, fields
from datetime import datetime

from codey.research.ledger import (
    EvidenceItem,
    OpenedSource,
    ResearchLedger,
    SearchRecord,
    SearchResultRecord,
    SourceQuality,
)
from codey.research.receipt_types import SourceSearchLocator, SourceSearchObservation
from codey.research.urls import parsed_url
from codey.types.json_value import (
    JsonObject,
    canonical_json_object,
    require_json_boolean,
    require_json_integer,
    require_json_string,
)


def decode_evidence_item(value: object) -> EvidenceItem:
    row = _record_fields(value, EvidenceItem)
    page = row["page"]
    return EvidenceItem(claim=require_json_string(row["claim"], "claim"),
        source_url=require_json_string(row["source_url"], "source_url"),
        excerpt=require_json_string(row["excerpt"], "excerpt"),
        stance=require_json_string(row["stance"], "stance"),
        note_id=require_json_string(row["note_id"], "note_id"),
        page=None if page is None else require_json_integer(page, "page"),
        locator=require_json_string(row["locator"], "locator"))


def _record_fields(value: object, model: type[SearchRecord] | type[SearchResultRecord] | type[OpenedSource] | type[SourceQuality] | type[EvidenceItem]) -> JsonObject:
    if not isinstance(value, dict) or set(value) != {item.name for item in fields(model)}:
        raise ValueError(f"invalid {model.__name__} observation fields")
    return canonical_json_object(value)


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
        results.append(SearchResultRecord(require_json_integer(hit["rank"], "rank"),
            require_json_string(hit["title"], "title"), require_json_string(hit["url"], "url"),
            require_json_string(hit["snippet"], "snippet")))
    return SearchRecord(query=require_json_string(row["query"], "query"), timestamp=require_json_string(row["timestamp"], "timestamp"), results=tuple(results))


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
    decoded_quality = SourceQuality(*(require_json_string(quality[key], key)
                                     for key in ("level", "kind", "freshness", "independent_group")))
    pages_read = row["pages_read"]
    if (not isinstance(pages_read, (list, tuple))
            or any(type(page) is not int or page < 1 for page in pages_read)
            or len(set(pages_read)) != len(pages_read)):
        raise ValueError("invalid source observation pages")
    if (type(row["truncated"]) is not bool or type(row["page_count"]) is not int or row["page_count"] < 0
            or any(type(row[key]) is not str for key in ("title", "content_kind", "mime_type", "text_hash"))):
        raise ValueError("invalid source observation metadata")
    source = OpenedSource(
        requested_url=require_json_string(row["requested_url"], "requested_url"),
        final_url=require_json_string(row["final_url"], "final_url"),
        title=require_json_string(row["title"], "title"),
        retrieved_at=require_json_string(row["retrieved_at"], "retrieved_at"),
        text_hash=require_json_string(row["text_hash"], "text_hash"), quality=decoded_quality,
        content_kind=require_json_string(row["content_kind"], "content_kind"),
        mime_type=require_json_string(row["mime_type"], "mime_type"),
        page_count=require_json_integer(row["page_count"], "page_count"),
        pages_read=tuple(require_json_integer(page, "page") for page in pages_read),
        truncated=require_json_boolean(row["truncated"], "truncated"))
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


def source_search_observation(observation: object) -> SourceSearchObservation:
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
    return SourceSearchObservation(source_url=row["source_url"], query=row["query"],
        hits=[SourceSearchLocator(offset=hit["offset"], page=hit["page"], snippet=hit["snippet"])
              for hit in row["hits"]])


def ledger_observation(ledger: ResearchLedger, kind: str, *, url: str = "", evidence_start: int = 0) -> JsonObject:
    observation: JsonObject
    if kind == "web_search" and ledger.searches:
        observation = canonical_json_object({"search": asdict(ledger.searches[-1])})
        search_observation(observation)
        return canonical_json_object(observation)
    if kind == "source_search" and ledger.source_searches:
        observation = canonical_json_object({"source_search": ledger.source_searches[-1]})
        source_search_observation(observation)
        return canonical_json_object(observation)
    if kind in {"knowledge_write", "save_finding"}:
        return canonical_json_object({"evidence": [asdict(item) for item in ledger.evidence_items[evidence_start:]]})
    source = ledger.source_record_for_url(url) if url else None
    if source is not None:
        observation = canonical_json_object({"source": asdict(source), "text": ledger.source_text_for_url(url),
                       "pages": [{"number": number, "text": text}
                                 for number, text in ledger.source_pages_for_url(url).items()]})
        source_observation(observation)
        return canonical_json_object(observation)
    raise ValueError(f"missing research observation for {kind}")


def restore_ledger_observation(ledger: ResearchLedger, observation: JsonObject) -> None:
    """Apply one verified receipt, preserving source identity and timestamps.

    The entry stages the whole replay on a clone and commits only after all
    receipts pass. Malformed source bodies never become read evidence.
    """
    if not isinstance(observation, dict):
        raise ValueError("research observation must be an object")
    keys = set(observation)
    if keys in ({"questions"}, {"questions", "question_details"}) or observation.get("schema_version") in (2, 3):
        from codey.research.questions import question_plan

        questions = observation["questions"]
        if observation.get("schema_version") in (2, 3):
            if not isinstance(questions, list) or any(not isinstance(row, dict) for row in questions):
                raise ValueError("invalid question observations")
            questions = [row["text"] for row in questions if isinstance(row, dict)]
        ledger.questions = question_plan(questions, ledger.questions)
    elif keys == {"search"}:
        ledger.searches.append(search_observation(observation))
    elif keys == {"source_search"}:
        ledger.source_searches.append(source_search_observation(observation))
    elif keys == {"source", "text", "pages"}:
        _restore_source(ledger, observation)
    elif keys == {"evidence"}:
        raw_items = observation["evidence"]
        if not isinstance(raw_items, list):
            raise ValueError("evidence observations must be an array")
        items = [decode_evidence_item(item) for item in raw_items]
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


def _restore_source(ledger: ResearchLedger, observation: JsonObject) -> None:
    source, text, page_map = source_observation(observation)
    ledger.merge_opened_source(source, text, page_map)
