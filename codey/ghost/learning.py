"""Attributed feedback and reviewable predictions, independent of task verdicts."""

from __future__ import annotations

import sqlite3

from codey.ghost.projection import record_from_payload, record_payload
from codey.ghost.storage import load, save
from codey.memory.codec import timestamp
from codey.memory.contracts import MemoryEvent
from codey.types.json_value import JsonObject, require_json_string


def _feedback(connection: sqlite3.Connection, event: MemoryEvent) -> None:
    item_id = require_json_string(event.payload.get("item_id"), "item_id")
    run_id = require_json_string(event.payload.get("run_id"), "run_id")
    rating = require_json_string(event.payload.get("rating"), "rating")
    if rating not in {"helpful", "unhelpful", "correction"}:
        raise ValueError("unknown feedback rating")
    exposure = load(connection, "exposures", run_id + ":" + item_id)
    raw = load(connection, "items", item_id)
    if raw is None or exposure is None or exposure.get("scope") != event.scope:
        raise ValueError("feedback must identify memory exposed to this run and scope")
    record = record_from_payload(raw)
    if timestamp(event.occurred_at) < timestamp(require_json_string(exposure.get("at"), "at")):
        raise ValueError("feedback cannot precede its exposure")
    # A user may judge one exposed item once per run; retries are journal-idempotent.
    if load(connection, "judgments", run_id + ":" + item_id) is not None:
        raise ValueError("this run already has feedback for the item")
    payload: JsonObject = {"id": event.event_id, "item_id": item_id, "run_id": run_id, "scope": event.scope,
               "rating": rating, "occurred_at": event.occurred_at,
               "source_refs": [*event.provenance, require_json_string(exposure.get("source_ref"), "source_ref")]}
    save(connection, "feedback", event.event_id, event.scope, payload)
    save(connection, "judgments", run_id + ":" + item_id, event.scope,
         {"id": run_id + ":" + item_id, "item_id": item_id, "scope": event.scope, "feedback_id": event.event_id})
    if rating == "correction" and record.state == "active":
        updated = record_payload(record)
        updated["state"] = "quarantined"
        updated["run_id"] = raw.get("run_id", "")
        save(connection, "items", item_id, record.item.scope, updated)


def _predict(connection: sqlite3.Connection, event: MemoryEvent) -> None:
    item_id = require_json_string(event.payload.get("item_id"), "item_id")
    statement = require_json_string(event.payload.get("statement"), "statement")
    expected = require_json_string(event.payload.get("expected_rating"), "expected_rating")
    due = require_json_string(event.payload.get("due_at"), "due_at")
    raw = load(connection, "items", item_id)
    if raw is None:
        raise ValueError("a prediction requires an existing confirmed memory")
    record = record_from_payload(raw)
    if record.state != "active" or record.item.scope != event.scope or not statement.strip():
        raise ValueError("prediction scope, statement or memory state is invalid")
    if expected not in {"helpful", "unhelpful", "correction"} or timestamp(due) <= timestamp(event.occurred_at):
        raise ValueError("prediction requires a future review window and an explicit expected rating")
    save(connection, "predictions", event.event_id, event.scope, {
        "id": event.event_id, "item_id": item_id, "scope": event.scope, "statement": statement,
        "expected_rating": expected, "created_at": event.occurred_at, "due_at": due,
        "status": "unjudged", "reviewed": False, "source_refs": list(event.provenance),
    })


def _review(connection: sqlite3.Connection, event: MemoryEvent) -> None:
    prediction_id = require_json_string(event.payload.get("prediction_id"), "prediction_id")
    feedback_id = require_json_string(event.payload.get("feedback_id"), "feedback_id")
    outcome = require_json_string(event.payload.get("outcome"), "outcome")
    prediction = load(connection, "predictions", prediction_id)
    if prediction is None or prediction.get("reviewed") is True:
        raise ValueError("prediction is missing or already reviewed")
    if timestamp(event.occurred_at) < timestamp(require_json_string(prediction.get("due_at"), "due_at")):
        raise ValueError("prediction review is not due")
    computed = "unjudged"
    if feedback_id:
        feedback = load(connection, "feedback", feedback_id)
        if feedback is None or feedback.get("item_id") != prediction.get("item_id") or feedback.get("scope") != prediction.get("scope"):
            raise ValueError("feedback does not belong to this prediction")
        observed = timestamp(require_json_string(feedback.get("occurred_at"), "occurred_at"))
        if not timestamp(require_json_string(prediction.get("created_at"), "created_at")) <= observed <= timestamp(require_json_string(prediction.get("due_at"), "due_at")):
            raise ValueError("feedback is outside the prediction window")
        computed = "supported" if feedback.get("rating") == prediction.get("expected_rating") else "contradicted"
    if computed != outcome:
        raise ValueError("review outcome differs from its attributable feedback")
    prediction.update(status=computed, reviewed=True, feedback_id=feedback_id,
                      reviewed_at=event.occurred_at, review_ref=event.provenance[0])
    scope = require_json_string(prediction.get("scope"), "scope")
    save(connection, "predictions", prediction_id, scope, prediction)


def project_learning(connection: sqlite3.Connection, event: MemoryEvent) -> None:
    if event.kind == "memory.feedback":
        _feedback(connection, event)
    elif event.kind == "memory.predicted":
        _predict(connection, event)
    else:
        _review(connection, event)
