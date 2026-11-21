"""Bounded, replayable maintenance and explicit retention."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import timedelta

from codey.ghost.controls import policy
from codey.ghost.storage import rows, save, texts
from codey.memory.codec import record_from_payload, record_payload, record_recency, timestamp
from codey.memory.contracts import MEMORY_POLICY_VERSION, MemoryEvent
from codey.types.json_value import (
    JsonObject,
    parse_json_object,
    require_json_integer,
    require_json_string,
    require_json_strings,
)


def project_batch(connection: sqlite3.Connection, event: MemoryEvent) -> None:
    if event.payload.get("policy_version") != MEMORY_POLICY_VERSION:
        raise ValueError("unsupported maintenance policy version")
    limit = require_json_integer(event.payload.get("max_items"), "max_items")
    cursor = require_json_string(event.payload.get("cursor"), "cursor")
    if not 1 <= limit <= 1000:
        raise ValueError("maintenance batch size must be between 1 and 1000")
    ordered = tuple(parse_json_object(value) for value in texts(connection.execute(
        "SELECT payload FROM projections WHERE collection = 'items' AND identity > ? ORDER BY identity LIMIT ?", (cursor, limit + 1))))
    selected = ordered[:limit]
    now = timestamp(event.occurred_at)
    for raw in selected:
        record = record_from_payload(raw)
        if (policy(connection, record.control_scopes).run_maintenance and record.state == "active"
                and record.item.expires_at and timestamp(record.item.expires_at) <= now):
            updated = record_payload(replace(record, state="expired", state_changed_at=event.occurred_at))
            updated["run_id"] = raw.get("run_id", "")
            save(connection, "items", record.item.item_id, record.item.scope, updated)
    next_cursor = record_from_payload(selected[-1]).item.item_id if selected else cursor
    complete = len(ordered) <= limit
    result: JsonObject = {"id": event.event_id, "scope": "user", "processed": len(selected), "cursor": "" if complete else next_cursor, "complete": complete, "at": event.occurred_at}
    save(connection, "maintenance", event.event_id, "user", result)
    save(connection, "cursors", "maintenance", "user", {"cursor": result["cursor"]})
    from codey.ghost.learning import review_due

    review_due(connection, event)


def project_retention(connection: sqlite3.Connection, event: MemoryEvent) -> None:
    maximum = require_json_integer(event.payload.get("max_experiences_per_scope"), "max_experiences_per_scope")
    days = require_json_integer(event.payload.get("experience_days"), "experience_days")
    archived = require_json_integer(event.payload.get("archived_days"), "archived_days")
    if min(maximum, days, archived) < 0:
        raise ValueError("retention limits cannot be negative")
    now = timestamp(event.occurred_at)
    counts: dict[str, int] = {}
    removed: set[str] = set()
    ordered = sorted(rows(connection, "items"), key=record_recency, reverse=True)
    for raw in ordered:
        record = record_from_payload(raw)
        item = record.item
        counts[item.scope] = counts.get(item.scope, 0) + (1 if item.kind == "experience" else 0)
        age = now - timestamp(item.created_at)
        evict = item.kind == "experience" and (counts[item.scope] > maximum or age > timedelta(days=days))
        archived_age = now - timestamp(record.state_changed_at or item.created_at)
        evict = evict or record.state != "active" and archived_age > timedelta(days=archived)
        if evict and policy(connection, record.control_scopes).run_maintenance:
            connection.execute("DELETE FROM projections WHERE collection = 'items' AND identity = ?", (item.item_id,))
            removed.add(item.item_id)
    for collection in ("proposals", "feedback", "predictions", "exposures", "judgments", "behaviors", "outcomes", "maintenance"):
        for row in rows(connection, collection):
            scopes = tuple(require_json_strings(row.get("control_scopes", []), "control_scopes"))
            scope_value = row.get("scope")
            if not policy(connection, scopes or ("user", scope_value if isinstance(scope_value, str) else "user")).run_maintenance:
                continue
            nested = row.get("item")
            item_id = row.get("item_id")
            if isinstance(nested, dict):
                item_id = nested.get("item_id")
            at = row.get("occurred_at", row.get("at", row.get("created_at")))
            if isinstance(nested, dict):
                at = nested.get("created_at")
            stale = isinstance(at, str) and now - timestamp(at) > timedelta(days=archived)
            if isinstance(item_id, str) and item_id in removed or stale:
                identity = item_id if collection == "proposals" else require_json_string(row.get("id"), "id")
                if not isinstance(identity, str):
                    raise ValueError("retention projection has no identity")
                connection.execute("DELETE FROM projections WHERE collection = ? AND identity = ?", (collection, identity))
    save(connection, "retention", "policy", "user", {**event.payload, "as_of": event.occurred_at})
