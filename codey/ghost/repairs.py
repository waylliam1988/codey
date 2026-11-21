"""Explicit credit links over real outcomes; hints never create new proof."""

from __future__ import annotations

import sqlite3

from codey.ghost.storage import load, save
from codey.memory.codec import timestamp
from codey.memory.contracts import MemoryEvent, MemoryItem
from codey.types.json_value import JsonObject, require_json_string, require_json_strings


def _outcome(connection: sqlite3.Connection, identity: str, scope: str) -> JsonObject:
    row = load(connection, "outcomes", identity)
    if row is None or row.get("scope") != scope:
        raise ValueError("repair must reference a real outcome in its scope")
    return row


def project_repair(connection: sqlite3.Connection, event: MemoryEvent) -> None:
    repair_id = require_json_string(event.payload.get("repair_id"), "repair_id")
    previous = load(connection, "repairs", repair_id)
    regression_id = require_json_string(event.payload.get("regression_id", ""), "regression_id")
    if regression_id:
        if previous is None:
            raise ValueError("repair is missing")
        observed = _outcome(connection, regression_id, event.scope)
        if observed.get("check_key") != previous.get("check_key") or observed.get("workspace_revision") != previous.get("workspace_revision"):
            raise ValueError("delayed regression belongs to a different check or revision")
        if timestamp(require_json_string(observed.get("at"), "at")) < timestamp(require_json_string(previous.get("at"), "at")):
            raise ValueError("regression precedes the repair")
        state = "contradicted" if observed.get("value") == "fail" else "supported" if observed.get("value") == "pass" else "unjudged"
        previous.update(status=state, regression_id=regression_id, review_ref=event.provenance[0])
        save(connection, "repairs", repair_id, event.scope, previous)
        if state == "contradicted":
            raw = load(connection, "items", "repair-candidate:" + repair_id)
            if raw is not None and raw.get("state") == "active":
                raw["state"] = "quarantined"
                raw["state_changed_at"] = event.occurred_at
                save(connection, "items", "repair-candidate:" + repair_id, event.scope, raw)
        return
    if previous is not None:
        if previous.get("source_ref") == event.provenance[0]:
            return
        raise ValueError("repair identity already exists")
    failure_id = require_json_string(event.payload.get("failure_id"), "failure_id")
    failure = _outcome(connection, failure_id, event.scope)
    if failure.get("status") not in {"error", "fail"}:
        raise ValueError("repair requires an observed original failure")
    actions = require_json_strings(event.payload.get("action_ids"), "action_ids")
    if not 1 <= len(actions) <= 8 or len(set(actions)) != len(actions):
        raise ValueError("repair requires unique bounded action references")
    check_id = require_json_string(event.payload.get("verification_id"), "verification_id")
    check = _outcome(connection, check_id, event.scope)
    revision = require_json_string(event.payload.get("workspace_revision"), "workspace_revision")
    if not revision or check.get("workspace_revision") != revision or check.get("category") != "completion_check":
        raise ValueError("repair requires an existing verification contract and exact workspace revision")
    sources = [*event.provenance, *require_json_strings(failure.get("source_refs", []), "source_refs"),
               *require_json_strings(check.get("source_refs", []), "source_refs")]
    for action_id in actions:
        action = _outcome(connection, action_id, event.scope)
        if action.get("category") != "tool_call" or action.get("run_id") != check.get("run_id"):
            raise ValueError("repair action must belong to the checked run")
        if not (timestamp(require_json_string(failure.get("at"), "at")) <= timestamp(require_json_string(action.get("at"), "at"))
                <= timestamp(require_json_string(check.get("at"), "at")) <= timestamp(event.occurred_at)):
            raise ValueError("repair outcomes are outside their causal time window")
        sources.extend(require_json_strings(action.get("source_refs", []), "source_refs"))
    statement = require_json_string(event.payload.get("statement"), "statement")
    if not statement.strip() or len(statement) > 1200:
        raise ValueError("repair statement exceeds its bounds")
    status = "supported" if check.get("value") == "pass" else "contradicted" if check.get("value") == "fail" else "unjudged"
    save(connection, "repairs", repair_id, event.scope, {"id": repair_id, "scope": event.scope,
        "failure_id": failure_id, "action_ids": [value for value in actions], "verification_id": check_id,
        "check_key": check.get("check_key"), "workspace_revision": revision, "statement": statement,
        "status": status, "at": event.occurred_at, "source_ref": event.provenance[0], "source_refs": [value for value in sources]})
    if status == "supported":
        item = MemoryItem("repair-candidate:" + repair_id, "hypothesis", "Prior repair experience, requiring fresh verification: " + statement,
            event.scope, tuple(sources), event.occurred_at, slot="repair.pattern", value=require_json_string(check.get("check_key"), "check_key"))
        save(connection, "proposals", item.item_id, event.scope, {"item": item.to_payload(), "decision": "",
            "control_scopes": ["user", event.scope], "relations": []})
