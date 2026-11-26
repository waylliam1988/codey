"""Lossless Research observations inside the existing tool-result receipts.

No log or executor: encode the affected ledger records and project them on
restart. Source bodies are carried by the managed result receipt, never by
UI ledger payloads (which deliberately clip excerpts and counts).
"""
from __future__ import annotations

from codey.research.ledger import (
    EvidenceItem,
    ResearchLedger,
    SearchRecord,
    SearchResultRecord,
)
from codey.research.receipt_types import SourceSearchLocator, SourceSearchObservation
from codey.research.search_provenance import decode_search_attempt
from codey.research.source_observation import (
    _record_fields,
    _source_url,
    _timestamp,
    source_observation,
    source_snapshot,
)
from codey.types.json_value import (
    JsonObject,
    canonical_json_object,
    encode_transport_value,
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


def search_observation(observation: object) -> SearchRecord:
    """Validate the exact search receipt for both live facts and recovery."""
    if not isinstance(observation, dict):
        raise ValueError("missing or invalid search observation")
    observation = canonical_json_object(observation)
    if set(observation) != {"search"}:
        raise ValueError("missing or invalid search observation")
    search = observation["search"]
    if isinstance(search, dict) and "version" not in search:
        search.setdefault("attempts", [])  # Legacy receipts did not observe search provenance.
        search.update(version=2, state="unknown", failure_kind="")
    row = _record_fields(search, SearchRecord)
    if require_json_integer(row["version"], "search version") != 2:
        raise ValueError("unsupported search record version")
    state = require_json_string(row["state"], "search state")
    if state not in {"unknown", "hits", "empty", "filtered_empty", "failed"}:
        raise ValueError("invalid search record state")
    failure = require_json_string(row["failure_kind"], "search failure")
    if (state == "failed") != bool(failure):
        raise ValueError("search failure differs from state")
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
    attempts = row["attempts"]
    if not isinstance(attempts, list):
        raise ValueError("invalid search attempts")
    decoded_attempts = tuple(decode_search_attempt(item) for item in attempts)
    if state != "unknown" and ((state == "hits") != bool(results)):
        raise ValueError("search state differs from its recorded results")
    if state == "empty" and not any(item.state == "empty" for item in decoded_attempts):
        raise ValueError("empty search requires a completed attempt")
    if state == "filtered_empty" and not any(item.rejected_urls for item in decoded_attempts):
        raise ValueError("filtered search requires rejected candidates")
    return SearchRecord(query=require_json_string(row["query"], "query"),
        timestamp=require_json_string(row["timestamp"], "timestamp"), results=tuple(results),
        attempts=decoded_attempts, state=state, failure_kind=failure)


def source_search_observation(observation: object) -> SourceSearchObservation:
    """Validate locator previews without interpreting their display text."""
    if not isinstance(observation, dict):
        raise ValueError("missing or invalid source search observation")
    observation = canonical_json_object(observation)
    if set(observation) != {"source_search"}:
        raise ValueError("missing or invalid source search observation")
    row = observation["source_search"]
    if not isinstance(row, dict) or set(row) != {"source_url", "source_snapshot", "query", "hits"}:
        raise ValueError("invalid source search observation fields")
    _source_url(row["source_url"])
    source, _text, _pages = source_observation(row["source_snapshot"])
    if source.final_url != row["source_url"]:
        raise ValueError("source search snapshot differs from its locator URL")
    if type(row["query"]) is not str or not row["query"].strip() or not isinstance(row["hits"], (list, tuple)):
        raise ValueError("invalid source search observation query or hits")
    hits: list[SourceSearchLocator] = []
    for hit in row["hits"]:
        if (not isinstance(hit, dict) or set(hit) != {"offset", "page", "snippet"}
                or type(hit["offset"]) is not int or hit["offset"] < 0
                or type(hit["snippet"]) is not str
                or hit["page"] is not None and (type(hit["page"]) is not int or hit["page"] < 1)):
            raise ValueError("invalid source search locator observation")
        text = _text if hit["page"] is None else _pages.get(hit["page"], "")
        if not 0 <= hit["offset"] < len(text):
            raise ValueError("source search locator is outside the captured source")
        hits.append(SourceSearchLocator(offset=require_json_integer(hit["offset"], "offset"),
            page=None if hit["page"] is None else require_json_integer(hit["page"], "page"),
            snippet=require_json_string(hit["snippet"], "snippet")))
    return SourceSearchObservation(source_url=require_json_string(row["source_url"], "source_url"),
        source_snapshot=canonical_json_object(row["source_snapshot"]), query=row["query"], hits=hits)


def ledger_observation(ledger: ResearchLedger, kind: str, *, url: str = "", evidence_start: int = 0) -> JsonObject:
    observation: JsonObject
    if kind == "web_search" and ledger.searches:
        observation = canonical_json_object({"search": encode_transport_value(ledger.searches[-1])})
        search_observation(observation)
        return canonical_json_object(observation)
    if kind == "source_search" and ledger.source_searches:
        observation = canonical_json_object({"source_search": ledger.source_searches[-1]})
        source_search_observation(observation)
        return canonical_json_object(observation)
    if kind in {"knowledge_write", "save_finding"}:
        return canonical_json_object({"evidence": [encode_transport_value(item) for item in ledger.evidence_items[evidence_start:]]})
    source = ledger.source_record_for_url(url) if url else None
    if source is not None:
        return source_snapshot(source, ledger.source_text_for_url(url), ledger.source_pages_for_url(url))
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
