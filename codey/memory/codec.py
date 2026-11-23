"""Strict codecs shared by the memory contract and its implementations."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime

from codey.memory.contracts import (
    MEMORY_POLICY_VERSION,
    MemoryEvent,
    MemoryItem,
    MemoryKind,
    MemoryRecord,
    MemoryState,
    MemoryTrace,
)
from codey.memory.slots import family_for, validate_slot
from codey.types.json_value import (
    JsonObject,
    decode_json_object,
    parse_json_object,
    require_json_integer,
    require_json_string,
    require_json_strings,
)


def timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("memory timestamps require a timezone")
    return parsed.astimezone(UTC)


def unique_scopes(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(value for index, value in enumerate(values) if value not in values[:index])


def canonical(value: object) -> str:
    return json.dumps(decode_json_object(value), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def exposure_identity(request_ref: str, item_id: str, version: str) -> str:
    return digest({"request": request_ref.removeprefix("request:"), "item": item_id, "version": version})


def item_from_payload(value: object) -> MemoryItem:
    row = decode_json_object(value)
    kinds: dict[str, MemoryKind] = {"experience": "experience", "preference": "preference",
                                   "goal": "goal", "hypothesis": "hypothesis"}
    kind = kinds.get(require_json_string(row.get("kind"), "kind"))
    if kind is None:
        raise ValueError("unknown memory kind")
    confidence = row.get("confidence")
    if confidence is not None and (isinstance(confidence, bool) or not isinstance(confidence, (int, float))
                                   or not math.isfinite(confidence) or not 0 <= confidence <= 1):
        raise ValueError("confidence must be a finite number between zero and one")
    result = MemoryItem(
        require_json_string(row.get("item_id"), "item_id"), kind,
        require_json_string(row.get("text"), "text"), require_json_string(row.get("scope"), "scope"),
        tuple(require_json_strings(row.get("source_refs"), "source_refs")),
        require_json_string(row.get("created_at"), "created_at"),
        require_json_string(row.get("expires_at", ""), "expires_at"),
        float(confidence) if confidence is not None else None,
        require_json_string(row.get("slot", ""), "slot"),
        require_json_string(row.get("context", ""), "context"),
        tuple(require_json_strings(row.get("supersedes", []), "supersedes")),
        require_json_string(row.get("value", ""), "value"),
    )
    if not result.item_id or not result.scope or not result.text.strip() or not result.source_refs:
        raise ValueError("memory items require identity, scope, text and sources")
    created = timestamp(result.created_at)
    if result.expires_at and timestamp(result.expires_at) <= created:
        raise ValueError("memory expiry must follow its creation")
    validate_slot(result)
    return result


def event_payload(event: MemoryEvent) -> JsonObject:
    timestamp(event.occurred_at)
    if not event.event_id or not event.scope or not event.provenance:
        raise ValueError("memory events require identity, provenance and scope")
    return {"event_id": event.event_id, "kind": event.kind, "occurred_at": event.occurred_at,
            "session_id": event.session_id, "project": event.project, "run_id": event.run_id,
            "scope": event.scope, "payload": decode_json_object(dict(event.payload)),
            "provenance": list(event.provenance), "control_scopes": list(event.control_scopes)}


def event_from_text(text: str) -> MemoryEvent:
    row = parse_json_object(text)
    return MemoryEvent(
        require_json_string(row.get("event_id"), "event_id"), require_json_string(row.get("kind"), "kind"),
        require_json_string(row.get("occurred_at"), "occurred_at"),
        require_json_string(row.get("session_id"), "session_id"),
        require_json_string(row.get("project"), "project"), require_json_string(row.get("run_id"), "run_id"),
        require_json_string(row.get("scope"), "scope"), decode_json_object(row.get("payload")),
        tuple(require_json_strings(row.get("provenance"), "provenance")),
        tuple(require_json_strings(row.get("control_scopes", []), "control_scopes")),
    )


def memory_state(value: str) -> MemoryState:
    states: dict[str, MemoryState] = {"active": "active", "superseded": "superseded", "revoked": "revoked",
                                     "quarantined": "quarantined", "expired": "expired"}
    state = states.get(value)
    if state is None:
        raise ValueError("invalid memory state")
    return state


def record_payload(record: MemoryRecord) -> JsonObject:
    return {"item": record.item.to_payload(), "state": record.state,
            "superseded_by": record.superseded_by, "control_scopes": list(record.control_scopes),
            "state_changed_at": record.state_changed_at}


def record_from_payload(value: JsonObject) -> MemoryRecord:
    return MemoryRecord(item_from_payload(value.get("item")),
                        memory_state(require_json_string(value.get("state"), "state")),
                        require_json_string(value.get("superseded_by"), "superseded_by"),
                        tuple(require_json_strings(value.get("control_scopes"), "control_scopes")),
                        require_json_string(value.get("state_changed_at", ""), "state_changed_at"))


def record_recency(value: JsonObject) -> tuple[datetime, str]:
    item = record_from_payload(value).item
    return timestamp(item.created_at), item.item_id


def trace_for_item(item: MemoryItem, reason: str, score: float = 0.0, rank: int = 0) -> MemoryTrace:
    return MemoryTrace(item.item_id, reason, score, rank, scope=item.scope, source_refs=item.source_refs,
                       family=family_for(item), item_version=digest(item.to_payload()))


def trace_from_payload(value: object) -> MemoryTrace:
    row = decode_json_object(value)
    score = row.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
        raise ValueError("memory trace score must be finite")
    return MemoryTrace(require_json_string(row.get("item_id"), "item_id"),
        require_json_string(row.get("reason"), "reason"), float(score),
        require_json_integer(row.get("rank"), "rank"), require_json_integer(row.get("chars"), "chars"),
        require_json_string(row.get("scope"), "scope"),
        tuple(require_json_strings(row.get("source_refs"), "source_refs")),
        require_json_string(row.get("family"), "family"),
        require_json_string(row.get("item_version", ""), "item_version"),
        require_json_string(row.get("family_state", ""), "family_state"),
        require_json_string(row.get("policy_version", MEMORY_POLICY_VERSION), "policy_version"),
        require_json_string(row.get("budget_unit", "characters"), "budget_unit"),
        tuple(require_json_strings(row.get("displaced_item_ids", []), "displaced_item_ids")),
        tuple(require_json_strings(row.get("conflict_refs", []), "conflict_refs")),
        require_json_string(row.get("consumer", ""), "consumer"))
