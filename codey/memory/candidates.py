"""Optional model metadata: bounded drafts, never execution arguments."""

from __future__ import annotations

from codey.memory.codec import item_from_payload
from codey.memory.contracts import MEMORY_CANDIDATE_LIMIT, MEMORY_CANDIDATE_TEXT_LIMIT, MemoryCandidateDraft
from codey.types.json_value import (
    JsonObject,
    decode_json_object,
    require_json_integer,
    require_json_string,
    require_json_strings,
)

CANDIDATE_FIELD = "memory_candidates"


def candidate_schema() -> JsonObject:
    properties: JsonObject = {
        "kind": {"type": "string", "enum": ["preference", "goal", "hypothesis"]},
        "text": {"type": "string", "minLength": 1, "maxLength": MEMORY_CANDIDATE_TEXT_LIMIT},
        "quote": {"type": "string", "minLength": 1, "maxLength": MEMORY_CANDIDATE_TEXT_LIMIT},
        "quote_start": {"type": "integer", "minimum": 0},
        "quote_end": {"type": "integer", "minimum": 1},
        "slot": {"type": "string", "maxLength": 80}, "value": {"type": "string", "maxLength": 256},
        "context": {"type": "string", "enum": ["", "chat", "project", "research"]},
        "expires_at": {"type": "string", "maxLength": 40},
        "related_item_ids": {"type": "array", "maxItems": 8, "items": {"type": "string", "maxLength": 256}},
    }
    return {"type": "array", "maxItems": MEMORY_CANDIDATE_LIMIT, "items": {
        "type": "object", "additionalProperties": False, "properties": properties,
        "required": ["kind", "text", "quote", "quote_start", "quote_end"],
    }}


def candidate_from_payload(value: object, *, user_text: str, scope: str, at: str) -> MemoryCandidateDraft:
    row = decode_json_object(value)
    schema = candidate_schema()
    item_schema = decode_json_object(schema["items"])
    properties = decode_json_object(item_schema["properties"])
    required = set(require_json_strings(item_schema["required"], "required"))
    if not required <= row.keys() or row.keys() - properties.keys():
        raise ValueError("invalid memory candidate fields")
    start = require_json_integer(row["quote_start"], "quote_start")
    end = require_json_integer(row["quote_end"], "quote_end")
    quote = require_json_string(row["quote"], "quote")
    text = require_json_string(row["text"], "text")
    if not 0 <= start < end <= len(user_text) or user_text[start:end] != quote:
        raise ValueError("memory candidate does not quote the bound user input")
    if not text.strip() or max(len(text), len(quote)) > MEMORY_CANDIDATE_TEXT_LIMIT:
        raise ValueError("memory candidate exceeds its text limit")
    item = item_from_payload({**row, "item_id": "candidate", "scope": scope,
                              "created_at": at, "source_refs": ["bound-user-input"]})
    if item.kind == "experience" or item.context not in {"", "chat", "project", "research"}:
        raise ValueError("unsupported memory candidate kind or context")
    related = tuple(require_json_strings(row.get("related_item_ids", []), "related_item_ids"))
    if len(related) > 8 or any(len(ref) > 256 for ref in related) or len(item.slot) > 80 or len(item.value) > 256 or len(item.expires_at) > 40:
        raise ValueError("memory candidate metadata exceeds its limits")
    return MemoryCandidateDraft(item.kind, text, quote, start, end, item.slot, item.value,
                                item.context, item.expires_at, related)
