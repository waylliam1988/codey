"""Deterministic memory command projection; no clock, model or execution access."""

from __future__ import annotations

import sqlite3
from dataclasses import replace

from codey.ghost.storage import load, rows, save
from codey.memory.codec import digest, item_from_payload, memory_state, timestamp
from codey.memory.contracts import MEMORY_CONTROLS, MemoryEvent, MemoryItem, MemoryPolicy, MemoryRecord
from codey.types.json_value import (
    JsonObject,
    decode_json_object,
    require_json_boolean,
    require_json_string,
    require_json_strings,
)


def record_payload(record: MemoryRecord) -> JsonObject:
    return {"item": record.item.to_payload(), "state": record.state,
            "superseded_by": record.superseded_by, "control_scopes": list(record.control_scopes)}


def record_from_payload(value: JsonObject) -> MemoryRecord:
    return MemoryRecord(item_from_payload(value.get("item")),
                        memory_state(require_json_string(value.get("state"), "state")),
                        require_json_string(value.get("superseded_by"), "superseded_by"),
                        tuple(require_json_strings(value.get("control_scopes"), "control_scopes")))


def policy(connection: sqlite3.Connection, scopes: tuple[str, ...]) -> MemoryPolicy:
    denied: set[str] = set()
    for row in rows(connection, "controls"):
        if row.get("scope") in scopes and row.get("enabled") is False:
            denied.add(require_json_string(row.get("name"), "name"))
    return MemoryPolicy(*(name not in denied for name in MEMORY_CONTROLS))


def _store_item(connection: sqlite3.Connection, record: MemoryRecord, run_id: str = "") -> None:
    payload = record_payload(record)
    payload["run_id"] = run_id
    save(connection, "items", record.item.item_id, record.item.scope, payload)


def _observe(connection: sqlite3.Connection, event: MemoryEvent) -> None:
    controls = event.control_scopes or ("user", event.scope)
    deleted = load(connection, "deleted_scopes", event.scope)
    if deleted is not None and timestamp(event.occurred_at) <= timestamp(require_json_string(deleted.get("at"), "at")):
        return
    if any(load(connection, "deleted_sources", ref) is not None for ref in event.provenance):
        return
    if not policy(connection, controls).accept_events:
        if event.kind == "memory.proposed":
            raise ValueError("memory ingestion is disabled in this scope")
        return
    if event.kind in {"task.completed", "task.failed"}:
        _task_facts(connection, event)
    if event.kind == "task.failed":
        return
    if event.kind == "task.completed":
        import json

        exchange: dict[str, str] = {"user": require_json_string(event.payload.get("user_text"), "user_text"),
                                     "assistant": require_json_string(event.payload.get("assistant_text"), "assistant_text")}
        text = json.dumps(exchange, ensure_ascii=False)
        item = MemoryItem(event.event_id, "experience", text, event.scope, event.provenance, event.occurred_at)
        _store_item(connection, MemoryRecord(item, control_scopes=controls), event.run_id)
        return
    payload = dict(event.payload)
    payload["item_id"] = event.event_id
    payload["scope"] = event.scope
    payload["source_refs"] = list(event.provenance)
    payload["created_at"] = event.occurred_at
    item = item_from_payload(payload)
    if item.kind == "experience":
        raise ValueError("experience is not a reviewable proposal")
    save(connection, "proposals", event.event_id, event.scope,
         {"item": item.to_payload(), "decision": "", "control_scopes": list(controls)})


def _task_facts(connection: sqlite3.Connection, event: MemoryEvent) -> None:
    exposures = event.payload.get("exposures", [])
    if not isinstance(exposures, list):
        raise ValueError("memory exposures must be an array")
    for value in exposures:
        row = decode_json_object(value)
        item_id = require_json_string(row.get("item_id"), "item_id")
        source = require_json_string(row.get("source_ref"), "source_ref")
        raw = load(connection, "items", item_id)
        if raw is None or not source:
            continue
        record = record_from_payload(raw)
        if record.item.scope not in {event.scope, "user"}:
            raise ValueError("memory exposure crosses its scope")
        save(connection, "exposures", event.run_id + ":" + item_id, record.item.scope,
             {"id": event.run_id + ":" + item_id, "item_id": item_id, "run_id": event.run_id, "scope": record.item.scope,
              "source_ref": source, "at": event.occurred_at})
    outcomes = event.payload.get("outcomes", [])
    if not isinstance(outcomes, list):
        raise ValueError("memory outcomes must be an array")
    for value in outcomes:
        row = decode_json_object(value)
        identity = require_json_string(row.get("effect_id"), "effect_id")
        save(connection, "outcomes", identity, event.scope,
             {**row, "id": identity, "run_id": event.run_id, "scope": event.scope, "source_refs": list(event.provenance)})


def _decide(connection: sqlite3.Connection, event: MemoryEvent) -> None:
    proposal_id = require_json_string(event.payload.get("proposal_id"), "proposal_id")
    version = require_json_string(event.payload.get("version"), "version")
    proposal = load(connection, "proposals", proposal_id)
    if proposal is None or proposal.get("decision") or digest(proposal.get("item")) != version:
        raise ValueError("proposal is missing, changed or already decided")
    item = item_from_payload(proposal.get("item"))
    if item.expires_at and timestamp(item.expires_at) <= timestamp(event.occurred_at):
        raise ValueError("proposal expired before confirmation")
    if require_json_boolean(event.payload.get("accepted"), "accepted"):
        targets = set(item.supersedes)
        for raw in rows(connection, "items"):
            old = record_from_payload(raw)
            same_slot = bool(item.slot and old.item.slot == item.slot and old.item.context == item.context
                             and old.item.kind == item.kind and old.item.scope == item.scope)
            if old.item.item_id in targets and (old.item.scope != item.scope or old.item.kind != item.kind):
                raise ValueError("corrections must name memory of the same kind and scope")
            if old.item.item_id in targets:
                targets.remove(old.item.item_id)
            if ((same_slot and old.state == "active") or
                    (old.item.item_id in item.supersedes and old.state in {"active", "quarantined", "revoked"})):
                _store_item(connection, replace(old, state="superseded", superseded_by=item.item_id))
        if targets:
            raise ValueError("correction target is missing")
        controls = tuple(require_json_strings(proposal.get("control_scopes"), "control_scopes"))
        _store_item(connection, MemoryRecord(replace(item, source_refs=(*item.source_refs, *event.provenance)),
                                            control_scopes=controls))
    proposal["decision"] = event.provenance[0]
    save(connection, "proposals", proposal_id, item.scope, proposal)


def _delete(connection: sqlite3.Connection, event: MemoryEvent) -> None:
    refs = set(require_json_strings(event.payload.get("source_refs", []), "source_refs"))
    deleted = 0
    removed_items: set[str] = set()
    for collection in ("items", "proposals", "feedback", "predictions", "exposures", "judgments", "outcomes"):
        for row in rows(connection, collection):
            item_row = row.get("item")
            item = item_from_payload(item_row) if item_row is not None else None
            scope = item.scope if item is not None else row.get("scope")
            sources = set(item.source_refs) if item is not None else set(require_json_strings(row.get("source_refs", []), "source_refs"))
            related = row.get("item_id")
            if (scope == event.scope or refs.intersection(sources)
                    or isinstance(related, str) and related in removed_items):
                identity = item.item_id if item is not None else require_json_string(row.get("id"), "id")
                connection.execute("DELETE FROM projections WHERE collection = ? AND identity = ?", (collection, identity))
                if collection == "items":
                    deleted += 1
                    removed_items.add(identity)
    prior = load(connection, "deleted_scopes", event.scope)
    if prior is None or timestamp(require_json_string(prior.get("at"), "at")) < timestamp(event.occurred_at):
        save(connection, "deleted_scopes", event.scope, event.scope, {"at": event.occurred_at})
    for ref in refs:
        save(connection, "deleted_sources", ref, event.scope, {"at": event.occurred_at})
    save(connection, "operations", event.event_id, event.scope, {"deleted": deleted})


def _change(connection: sqlite3.Connection, event: MemoryEvent) -> None:
    identity = require_json_string(event.payload.get("item_id"), "item_id")
    raw = load(connection, "items", identity)
    if raw is None:
        raise ValueError("memory item does not exist")
    record = record_from_payload(raw)
    state = memory_state(require_json_string(event.payload.get("state"), "state"))
    if record.item.scope != event.scope or state not in {"active", "revoked", "quarantined"}:
        raise ValueError("invalid memory lifecycle change")
    if state == "active" and (record.state == "superseded" or
            record.item.expires_at and timestamp(record.item.expires_at) <= timestamp(event.occurred_at)):
        raise ValueError("superseded or expired memory requires a new confirmed proposal")
    _store_item(connection, replace(record, state=state), require_json_string(raw.get("run_id"), "run_id"))


def _control(connection: sqlite3.Connection, event: MemoryEvent) -> None:
    name = require_json_string(event.payload.get("name"), "name")
    if name not in MEMORY_CONTROLS:
        raise ValueError("unknown memory control")
    enabled = require_json_boolean(event.payload.get("enabled"), "enabled")
    save(connection, "controls", event.scope + ":" + name, event.scope,
         {"scope": event.scope, "name": name, "enabled": enabled, "source_refs": list(event.provenance)})


def _maintain(connection: sqlite3.Connection, event: MemoryEvent) -> None:
    now = timestamp(event.occurred_at)
    for raw in rows(connection, "items"):
        record = record_from_payload(raw)
        if (policy(connection, record.control_scopes).run_maintenance and record.state == "active"
                and record.item.expires_at and timestamp(record.item.expires_at) <= now):
            _store_item(connection, replace(record, state="expired"), require_json_string(raw.get("run_id"), "run_id"))


def project_event(connection: sqlite3.Connection, event: MemoryEvent) -> None:
    if event.kind in {"task.completed", "task.failed", "memory.proposed"}:
        _observe(connection, event)
    elif event.kind == "memory.decided":
        _decide(connection, event)
    elif event.kind == "memory.deleted":
        _delete(connection, event)
    elif event.kind == "memory.changed":
        _change(connection, event)
    elif event.kind == "memory.controlled":
        _control(connection, event)
    elif event.kind == "memory.maintained":
        _maintain(connection, event)
    elif event.kind in {"memory.feedback", "memory.predicted", "memory.reviewed"}:
        from codey.ghost.learning import project_learning

        project_learning(connection, event)
    else:
        raise ValueError("unknown memory event kind")
