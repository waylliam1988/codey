"""Deterministic memory command projection; no clock, model or execution access."""

from __future__ import annotations

import sqlite3
from dataclasses import replace

from codey.ghost.conflicts import relation
from codey.ghost.controls import policy
from codey.ghost.storage import load, rows, save
from codey.memory.codec import digest, item_from_payload, memory_state, record_from_payload, record_payload, timestamp
from codey.memory.contracts import (
    MEMORY_CONTROLS,
    MEMORY_EXPERIENCE_EXCERPT_CHARS,
    MemoryEvent,
    MemoryItem,
    MemoryRecord,
)
from codey.types.json_value import (
    decode_json_object,
    require_json_boolean,
    require_json_string,
    require_json_strings,
)


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
        if any(len(value) > MEMORY_EXPERIENCE_EXCERPT_CHARS for value in exchange.values()):
            exchange = {key: value[:MEMORY_EXPERIENCE_EXCERPT_CHARS] for key, value in exchange.items()}
            exchange["excerpt"] = "truncated; source references identify the complete original messages"
        text = json.dumps(exchange, ensure_ascii=False)
        item = MemoryItem(event.event_id, "experience", text, event.scope, event.provenance, event.occurred_at)
        _store_item(connection, MemoryRecord(item, control_scopes=controls), event.run_id)
        return
    payload = dict(event.payload)
    if load(connection, "proposals", event.event_id) is not None or load(connection, "items", event.event_id) is not None:
        return
    payload["item_id"] = event.event_id
    payload["scope"] = event.scope
    payload["source_refs"] = list(event.provenance)
    payload["created_at"] = event.occurred_at
    item = item_from_payload(payload)
    if item.kind == "experience":
        raise ValueError("experience is not a reviewable proposal")
    save(connection, "proposals", event.event_id, event.scope,
         {"item": item.to_payload(), "decision": "", "control_scopes": list(controls),
          "relations": [edge.to_payload() for raw in rows(connection, "items")
                        if (old := record_from_payload(raw)).state in {"active", "quarantined", "revoked"}
                        and (edge := relation(item, old.item,
                            suggested=old.item.item_id in require_json_strings(event.payload.get("related_item_ids", []), "related_item_ids"))) is not None]})


def _candidate_proposals(connection: sqlite3.Connection, event: MemoryEvent) -> None:
    if not policy(connection, event.control_scopes or ("user", event.scope)).accept_events:
        return
    candidates = event.payload.get("candidates", [])
    if not isinstance(candidates, list):
        raise ValueError("candidate projection requires an array")
    for value in candidates:
        row = decode_json_object(value)
        identity = "candidate:" + digest({"parent": event.event_id, "response": row.get("response_ref"), "index": row.get("index")})
        _observe(connection, replace(event, event_id=identity, kind="memory.proposed", payload=row))


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
        identity = require_json_string(row.get("id", event.run_id + ":" + item_id), "id")
        version = require_json_string(row.get("item_version", digest(record.item.to_payload())), "item_version")
        save(connection, "exposures", identity, record.item.scope,
             {"id": identity, "item_id": item_id, "item_version": version, "run_id": event.run_id, "scope": record.item.scope,
              "context": row.get("context", ""),
              "control_scopes": list(record.control_scopes),
              "source_ref": source, "at": event.occurred_at})
    outcomes = event.payload.get("outcomes", [])
    if not isinstance(outcomes, list):
        raise ValueError("memory outcomes must be an array")
    for value in outcomes:
        row = decode_json_object(value)
        identity = require_json_string(row.get("effect_id"), "effect_id")
        save(connection, "outcomes", identity, event.scope,
             {**row, "id": identity, "at": row.get("at", event.occurred_at), "run_id": event.run_id, "scope": event.scope, "source_refs": list(event.provenance), "control_scopes": list(event.control_scopes)})
    behaviors = event.payload.get("behaviors", [])
    if not isinstance(behaviors, list):
        raise ValueError("behaviors must be an array")
    for value in behaviors:
        row = decode_json_object(value)
        save(connection, "behaviors", require_json_string(row.get("id"), "id"), event.scope,
             {**row, "scope": event.scope, "at": event.occurred_at, "source_refs": list(event.provenance), "control_scopes": list(event.control_scopes)})


def _decide(connection: sqlite3.Connection, event: MemoryEvent) -> None:
    proposal_id = require_json_string(event.payload.get("proposal_id"), "proposal_id")
    version = require_json_string(event.payload.get("version"), "version")
    proposal = load(connection, "proposals", proposal_id)
    settled = load(connection, "decisions", proposal_id)
    if settled is not None and settled.get("source_ref") == event.provenance[0]:
        if settled.get("version") != version or settled.get("accepted") != event.payload.get("accepted"):
            raise ValueError("settled memory decision changed")
        return
    if proposal is None or proposal.get("decision") or digest(proposal.get("item")) != version:
        raise ValueError("proposal is missing, changed or already decided")
    item = item_from_payload(proposal.get("item"))
    replacements = tuple(require_json_strings(event.payload.get("supersedes", []), "supersedes"))
    if replacements:
        unique_targets: dict[str, None] = dict.fromkeys((*item.supersedes, *replacements))
        item = replace(item, supersedes=tuple(unique_targets))
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
            if ((same_slot and old.state in {"active", "quarantined", "revoked"}) or
                    (old.item.item_id in item.supersedes and old.state in {"active", "quarantined", "revoked"})):
                _store_item(connection, replace(old, state="superseded", superseded_by=item.item_id, state_changed_at=event.occurred_at))
        if targets:
            raise ValueError("correction target is missing")
        controls = tuple(require_json_strings(proposal.get("control_scopes"), "control_scopes"))
        _store_item(connection, MemoryRecord(replace(item, source_refs=(*item.source_refs, *event.provenance)),
                                            control_scopes=controls))
        if item.slot == "code.language":
            from datetime import timedelta

            due = (timestamp(event.occurred_at) + timedelta(days=7)).isoformat()
            save(connection, "predictions", "adaptation:" + item.item_id, item.scope, {
                "id": "adaptation:" + item.item_id, "item_id": item.item_id, "scope": item.scope,
                "item_version": digest(replace(item, source_refs=(*item.source_refs, *event.provenance)).to_payload()),
                "statement": "Future exposed answers will label code examples as " + item.value,
                "expected_rating": "helpful", "target_kind": "behavior", "target_key": "code_fence_language",
                "expected_value": item.value, "workspace_revision": "", "created_at": event.occurred_at,
                "due_at": due, "status": "unjudged", "reviewed": False, "source_refs": list(item.source_refs),
                "control_scopes": list(controls)})
    proposal["decision"] = event.provenance[0]
    save(connection, "proposals", proposal_id, item.scope, proposal)
    save(connection, "decisions", proposal_id, item.scope, {"id": proposal_id, "scope": item.scope,
        "version": version, "accepted": event.payload.get("accepted"), "source_ref": event.provenance[0]})


def _delete(connection: sqlite3.Connection, event: MemoryEvent) -> None:
    if load(connection, "operations", event.event_id) is not None:
        return
    watermark = load(connection, "deleted_scopes", event.scope)
    if watermark is not None and timestamp(require_json_string(watermark.get("at"), "at")) > timestamp(event.occurred_at):
        save(connection, "operations", event.event_id, event.scope, {"deleted": 0})
        return
    refs = set(require_json_strings(event.payload.get("source_refs", []), "source_refs"))
    deleted = 0
    removed_items: set[str] = set()
    for collection in ("items", "proposals", "feedback", "predictions", "exposures", "judgments", "outcomes", "behaviors", "learning", "repairs"):
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
    if state == "active" and record.item.slot and any(
        (other := record_from_payload(value)).state == "active" and other.item.item_id != identity
        and other.item.scope == event.scope and other.item.kind == record.item.kind
        and other.item.slot == record.item.slot and other.item.context == record.item.context
        for value in rows(connection, "items")):
        raise ValueError("restore conflicts with confirmed memory; use a new proposal")
    _store_item(connection, replace(record, state=state, state_changed_at=event.occurred_at), require_json_string(raw.get("run_id"), "run_id"))


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
            _store_item(connection, replace(record, state="expired", state_changed_at=event.occurred_at), require_json_string(raw.get("run_id"), "run_id"))


def project_event(connection: sqlite3.Connection, event: MemoryEvent) -> None:
    if event.kind in {"task.completed", "task.failed", "memory.proposed"}:
        _observe(connection, event)
        if event.kind == "task.completed":
            _candidate_proposals(connection, event)
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
    elif event.kind == "memory.family_changed":
        from codey.ghost.families import project_family

        project_family(connection, event)
    elif event.kind == "memory.evaluated":
        from codey.ghost.families import project_evaluation

        project_evaluation(connection, event)
    elif event.kind == "memory.repair_reviewed":
        from codey.ghost.repairs import project_repair

        project_repair(connection, event)
    elif event.kind == "memory.observation_checkpoint":
        from codey.types.json_value import require_json_integer

        sequence = require_json_integer(event.payload.get("sequence"), "sequence")
        previous = load(connection, "cursors", "core")
        old = require_json_integer(previous.get("sequence"), "sequence") if previous else 0
        if sequence < old:
            raise ValueError("observation cursor cannot move backwards")
        save(connection, "cursors", "core", "user", {"sequence": sequence})
    elif event.kind in {"memory.maintenance_batch", "memory.retained"}:
        from codey.ghost.maintenance import project_batch, project_retention

        if event.kind == "memory.retained":
            project_retention(connection, event)
        else:
            project_batch(connection, event)
    else:
        raise ValueError("unknown memory event kind")
