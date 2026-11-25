"""Encode and validate immutable source observations independently of the research ledger."""
from __future__ import annotations

import hashlib
import re
from dataclasses import Field, fields, is_dataclass
from datetime import datetime

from codey.research.source_document import OpenedSource, PublicationMetadata, SourceQuality, SourceReadScope
from codey.research.urls import parsed_url
from codey.types.json_value import (
    JsonObject,
    canonical_json_object,
    encode_transport_value,
    require_json_boolean,
    require_json_integer,
    require_json_string,
)


def _record_fields(value: object, model: type[object]) -> JsonObject:
    if not isinstance(value, dict):
        raise ValueError(f"invalid {model.__name__} observation fields")
    row = canonical_json_object(value)
    if model is OpenedSource:
        row.setdefault("read_scope", None)
        row.setdefault("publication", None)
        row.setdefault("pdf_sha256", "")
    if not is_dataclass(model):
        raise TypeError("observation model must be a dataclass")
    source_fields: tuple[Field[object], ...] = fields(model)
    if set(row) != {item.name for item in source_fields}:
        raise ValueError(f"invalid {model.__name__} observation fields")
    return row


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


def source_observation(observation: object) -> tuple[OpenedSource, str, dict[int, str]]:
    """Decode one source observation without mutating any task or ledger."""
    if not isinstance(observation, dict):
        raise ValueError("missing or invalid source observation")
    observation = canonical_json_object(observation)
    if set(observation) != {"source", "text", "pages"}:
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
    pdf_sha256 = row["pdf_sha256"]
    if not isinstance(pdf_sha256, str) or pdf_sha256 and (
            row["content_kind"] != "pdf" or re.fullmatch(r"[0-9a-f]{64}", pdf_sha256) is None):
        raise ValueError("invalid PDF document hash")
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
        truncated=require_json_boolean(row["truncated"], "truncated"),
        read_scope=_read_scope(row["read_scope"], observation["text"]),
        publication=PublicationMetadata.decode(row["publication"]) if row["publication"] is not None else None,
        pdf_sha256=pdf_sha256)
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


def _read_scope(value: object, text: object) -> SourceReadScope | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("invalid source read scope")
    value = canonical_json_object(value)
    if (set(value) != {"text", "start", "end"}
            or not isinstance(text, str) or not isinstance(value["text"], str)):
        raise ValueError("invalid source read scope")
    scope = SourceReadScope.locate(text, value["text"])
    if (value["text"] != scope.text
            or any(value[key] is not None and type(value[key]) is not int for key in ("start", "end"))
            or value["start"] != scope.start or value["end"] != scope.end):
        raise ValueError("source read scope differs from captured text")
    return scope


def source_snapshot(source: OpenedSource, text: str, pages: dict[int, str]) -> JsonObject:
    """Encode and validate the same source capture for acquisition and recovery."""
    metadata = canonical_json_object(encode_transport_value(source))
    if source.publication is not None:
        metadata["publication"] = source.publication.to_payload()
    payload: dict[str, object] = {"source": metadata, "text": text,
        "pages": [{"number": number, "text": body} for number, body in pages.items()]}
    observation = canonical_json_object(payload)
    source_observation(observation)
    return observation
