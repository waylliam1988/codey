"""Attributed feedback and reviewable predictions, independent of task verdicts."""

from __future__ import annotations

import sqlite3

from codey.ghost.controls import policy
from codey.ghost.storage import load, rows, save
from codey.memory.codec import digest, exposure_identity, record_from_payload, record_payload, timestamp, unique_scopes
from codey.memory.contracts import MemoryEvent, MemoryItem, MemoryRecord
from codey.memory.slots import contextual_slot_correction
from codey.types.json_value import JsonObject, require_json_integer, require_json_string, require_json_strings


def _feedback(connection: sqlite3.Connection, event: MemoryEvent) -> None:
    item_id = require_json_string(event.payload.get("item_id"), "item_id")
    run_id = require_json_string(event.payload.get("run_id"), "run_id")
    rating = require_json_string(event.payload.get("rating"), "rating")
    if rating not in {"helpful", "unhelpful", "correction"}:
        raise ValueError("unknown feedback rating")
    exposure_id = require_json_string(event.payload.get("exposure_id", ""), "exposure_id")
    matches = [row for row in rows(connection, "exposures") if row.get("run_id") == run_id and row.get("item_id") == item_id]
    exposure = load(connection, "exposures", exposure_id) if exposure_id else matches[0] if len(matches) == 1 else None
    raw = load(connection, "items", item_id)
    if raw is None or exposure is None or exposure.get("scope") != event.scope:
        raise ValueError("feedback must identify memory exposed to this run and scope")
    record = record_from_payload(raw)
    item_version = digest(record.item.to_payload())
    if exposure.get("item_version") != item_version or exposure.get("run_id") != run_id or exposure.get("item_id") != item_id:
        raise ValueError("feedback exposure names a different memory version or run")
    behavior_id = require_json_string(event.payload.get("behavior_id", ""), "behavior_id")
    if behavior_id:
        behavior = load(connection, "behaviors", behavior_id)
        if behavior is None or behavior.get("run_id") != run_id or behavior.get("request_ref") != exposure.get("source_ref"):
            raise ValueError("feedback behavior does not belong to this exposure")
    if timestamp(event.occurred_at) < timestamp(require_json_string(exposure.get("at"), "at")):
        raise ValueError("feedback cannot precede its exposure")
    # A user may judge one exposed item once per run; retries are journal-idempotent.
    judgment = load(connection, "judgments", run_id + ":" + item_id)
    if judgment is not None and judgment.get("feedback_id") == event.event_id:
        return
    if judgment is not None:
        raise ValueError("this run already has feedback for the item")
    request_ref = require_json_string(exposure.get("source_ref"), "source_ref")
    correction = require_json_string(event.payload.get("correction_text", ""), "correction_text")
    if correction and rating != "correction":
        raise ValueError("only correction feedback carries correction text")
    payload: JsonObject = {"id": event.event_id, "item_id": item_id, "run_id": run_id, "scope": event.scope,
               "rating": rating, "occurred_at": event.occurred_at,
               "exposure_id": exposure.get("id"), "item_version": exposure.get("item_version"), "behavior_id": behavior_id,
               "attribution": "observed_behavior" if behavior_id else "exposure_only",
               "control_scopes": list(record.control_scopes),
               "source_refs": [*event.provenance, request_ref]}
    if correction:
        payload["correction_text"] = correction
    save(connection, "feedback", event.event_id, event.scope, payload)
    save(connection, "judgments", run_id + ":" + item_id, event.scope,
         {"id": run_id + ":" + item_id, "item_id": item_id, "scope": event.scope, "feedback_id": event.event_id, "control_scopes": list(record.control_scopes)})
    context = require_json_string(exposure.get("context", ""), "context")
    learning_id = item_id + ":" + context
    previous = load(connection, "learning", learning_id)
    helpful = require_json_integer(previous.get("helpful"), "helpful") if previous else 0
    negative = require_json_integer(previous.get("negative"), "negative") if previous else 0
    save(connection, "learning", learning_id, event.scope, {"id": learning_id, "item_id": item_id, "context": context,
        "scope": event.scope, "item_version": exposure.get("item_version"),
        "helpful": min(1000000, helpful + (1 if rating == "helpful" else 0)),
        "negative": min(1000000, negative + (0 if rating == "helpful" else 1)),
        "last_feedback_id": event.event_id, "source_refs": list(event.provenance), "at": event.occurred_at})
    if record.state != "active" or rating != "correction":
        return
    if correction:
        if request_ref.startswith("request:") and exposure_id == exposure_identity(request_ref, item_id, item_version):
            _contextual_correction(connection, event, record, correction, request_ref, context)
    else:
        updated = record_payload(record)
        updated["state"] = "quarantined"
        updated["state_changed_at"] = event.occurred_at
        updated["run_id"] = raw.get("run_id", "")
        save(connection, "items", item_id, record.item.scope, updated)


def _contextual_correction(connection: sqlite3.Connection, event: MemoryEvent, record: MemoryRecord,
                           text: str, request_ref: str, context: str) -> None:
    old = record.item
    correction = contextual_slot_correction(text)
    scope = require_json_string(event.payload.get("context_scope", ""), "context_scope")
    if (correction is None or old.kind != "preference" or old.slot != correction.slot or old.value == correction.value
            or context != "project" or not scope.startswith("project:") or old.scope not in {"user", scope}):
        return
    identity = "correction-candidate:" + event.event_id
    refs = tuple({ref: None for ref in (*event.provenance, request_ref)})
    item_refs = tuple({ref: None for ref in (*old.source_refs, *refs)})
    item = MemoryItem(identity, "preference", text, scope, item_refs,
                      event.occurred_at, slot=old.slot, context=context, value=correction.value)
    save(connection, "proposals", identity, scope, {"item": item.to_payload(), "decision": "",
        "control_scopes": list(unique_scopes(("user", scope, *record.control_scopes))),
        "relations": [{"target_id": old.item_id, "relation": "context_exception", "source_refs": list(refs)}]})


def _predict(connection: sqlite3.Connection, event: MemoryEvent) -> None:
    item_id = require_json_string(event.payload.get("item_id"), "item_id")
    statement = require_json_string(event.payload.get("statement"), "statement")
    expected = require_json_string(event.payload.get("expected_rating"), "expected_rating")
    due = require_json_string(event.payload.get("due_at"), "due_at")
    raw = load(connection, "items", item_id)
    if raw is None:
        raise ValueError("a prediction requires an existing confirmed memory")
    record = record_from_payload(raw)
    if record.item.expires_at and timestamp(record.item.expires_at) <= timestamp(event.occurred_at):
        raise ValueError("prediction memory has expired")
    target = require_json_string(event.payload.get("target_kind", "feedback"), "target_kind")
    key = require_json_string(event.payload.get("target_key", ""), "target_key")
    expected_value = require_json_string(event.payload.get("expected_value", ""), "expected_value")
    if target not in {"feedback", "behavior", "verification"} or target != "feedback" and (not key or not expected_value):
        raise ValueError("a prediction requires a supported observable target")
    if record.state != "active" or record.item.scope != event.scope or not statement.strip():
        raise ValueError("prediction scope, statement or memory state is invalid")
    if expected not in {"helpful", "unhelpful", "correction"} or timestamp(due) <= timestamp(event.occurred_at):
        raise ValueError("prediction requires a future review window and an explicit expected rating")
    save(connection, "predictions", event.event_id, event.scope, {
        "id": event.event_id, "item_id": item_id, "scope": event.scope, "statement": statement,
        "expected_rating": expected, "created_at": event.occurred_at, "due_at": due,
        "status": "unjudged", "reviewed": False, "source_refs": list(event.provenance),
        "item_version": digest(record.item.to_payload()), "control_scopes": list(record.control_scopes), "target_kind": target, "target_key": key,
        "expected_value": expected_value, "workspace_revision": event.payload.get("workspace_revision", ""),
    })


def _review(connection: sqlite3.Connection, event: MemoryEvent) -> None:
    prediction_id = require_json_string(event.payload.get("prediction_id"), "prediction_id")
    feedback_id = require_json_string(event.payload.get("feedback_id"), "feedback_id")
    outcome = require_json_string(event.payload.get("outcome"), "outcome")
    prediction = load(connection, "predictions", prediction_id)
    if prediction is not None and prediction.get("reviewed") is True and prediction.get("review_ref") == event.provenance[0]:
        return
    if prediction is None or prediction.get("reviewed") is True:
        raise ValueError("prediction is missing or already reviewed")
    if timestamp(event.occurred_at) < timestamp(require_json_string(prediction.get("due_at"), "due_at")):
        raise ValueError("prediction review is not due")
    computed = "unjudged"
    if feedback_id:
        target = prediction.get("target_kind", "feedback")
        collection = "feedback" if target == "feedback" else "behaviors" if target == "behavior" else "outcomes"
        feedback = load(connection, collection, feedback_id)
        if feedback is None or target == "feedback" and feedback.get("scope") != prediction.get("scope"):
            raise ValueError("feedback does not belong to this prediction")
        if target == "feedback" and (feedback.get("item_id") != prediction.get("item_id")
                or feedback.get("item_version") != prediction.get("item_version")):
            raise ValueError("feedback does not belong to this prediction version")
        if target != "feedback":
            _validate_observation(connection, prediction, feedback, target)
        observed = timestamp(require_json_string(feedback.get("occurred_at", feedback.get("at")), "observed_at"))
        if not timestamp(require_json_string(prediction.get("created_at"), "created_at")) <= observed <= timestamp(require_json_string(prediction.get("due_at"), "due_at")):
            raise ValueError("feedback is outside the prediction window")
        actual = feedback.get("rating") if target == "feedback" else feedback.get("value")
        expected = prediction.get("expected_rating") if target == "feedback" else prediction.get("expected_value")
        computed = "unjudged" if actual is None or actual == "unknown" else "supported" if actual == expected else "contradicted"
    if computed != outcome:
        raise ValueError("review outcome differs from its attributable feedback")
    prediction.update(status=computed, reviewed=True, feedback_id=feedback_id,
                      reviewed_at=event.occurred_at, review_ref=event.provenance[0])
    scope = require_json_string(prediction.get("scope"), "scope")
    save(connection, "predictions", prediction_id, scope, prediction)
    if computed == "contradicted":
        _review_proposal(connection, prediction, event)


def _review_proposal(connection: sqlite3.Connection, prediction: JsonObject, event: MemoryEvent) -> None:
    from datetime import timedelta

    raw = load(connection, "items", require_json_string(prediction.get("item_id"), "item_id"))
    if raw is None:
        return
    record = record_from_payload(raw)
    identity = "review-candidate:" + require_json_string(prediction.get("id"), "id")
    target = prediction.get("target_kind", "feedback")
    collection = "feedback" if target == "feedback" else "behaviors" if target == "behavior" else "outcomes"
    observation = load(connection, collection, require_json_string(prediction.get("feedback_id"), "feedback_id"))
    references = tuple(require_json_strings(observation.get("source_refs", []), "source_refs")) if observation else ()
    item = MemoryItem(identity, "hypothesis", "Consider a context exception: the expectation '"
        + require_json_string(prediction.get("statement"), "statement") + "' was contradicted by its matched observation.",
        record.item.scope, (*record.item.source_refs, *references), event.occurred_at,
        expires_at=(timestamp(event.occurred_at) + timedelta(days=7)).isoformat(), context=record.item.context)
    save(connection, "proposals", identity, record.item.scope, {"item": item.to_payload(), "decision": "",
        "control_scopes": list(record.control_scopes), "relations": [], "review_ref": prediction.get("id")})


def _validate_observation(connection: sqlite3.Connection, prediction: JsonObject, observed: JsonObject, target: object) -> None:
    key = "kind" if target == "behavior" else "check_key"
    if observed.get(key) != prediction.get("target_key"):
        raise ValueError("observation differs from the prediction target")
    revision = prediction.get("workspace_revision")
    if revision and observed.get("workspace_revision") != revision:
        raise ValueError("observation belongs to another workspace revision")
    if not any(row.get("item_id") == prediction.get("item_id") and row.get("item_version") == prediction.get("item_version")
               and row.get("run_id") == observed.get("run_id") and row.get("scope") == prediction.get("scope")
               and (target != "behavior" or row.get("source_ref") == observed.get("request_ref"))
               for row in rows(connection, "exposures")):
        raise ValueError("observation has no matching versioned exposure")


def review_due(connection: sqlite3.Connection, event: MemoryEvent) -> None:
    from dataclasses import replace

    from codey.ghost.storage import texts
    from codey.types.json_value import parse_json_object

    due = tuple(parse_json_object(value) for value in texts(connection.execute(
        "SELECT payload FROM projections WHERE collection = 'predictions' AND json_extract(payload, '$.reviewed') = 0 "
        "AND julianday(json_extract(payload, '$.due_at')) <= julianday(?) ORDER BY identity LIMIT 100", (event.occurred_at,))))
    for prediction in due:
        scopes = tuple(require_json_strings(prediction.get("control_scopes", []), "control_scopes"))
        if not policy(connection, scopes or ("user", require_json_string(prediction.get("scope"), "scope"))).run_maintenance:
            continue
        if prediction.get("reviewed") is True or timestamp(require_json_string(prediction.get("due_at"), "due_at")) > timestamp(event.occurred_at):
            continue
        target = prediction.get("target_kind", "feedback")
        collection = "feedback" if target == "feedback" else "behaviors" if target == "behavior" else "outcomes"
        matches = []
        for row in rows(connection, collection):
            observed_at = row.get("occurred_at", row.get("at"))
            if not isinstance(observed_at, str) or target == "feedback" and row.get("scope") != prediction.get("scope"):
                continue
            if not timestamp(require_json_string(prediction.get("created_at"), "created_at")) <= timestamp(observed_at) <= timestamp(require_json_string(prediction.get("due_at"), "due_at")):
                continue
            if target == "feedback":
                if row.get("item_id") != prediction.get("item_id") or row.get("item_version") != prediction.get("item_version"):
                    continue
            else:
                try:
                    _validate_observation(connection, prediction, row, target)
                except ValueError:
                    continue
            matches.append(row)
        matches.sort(key=lambda row: timestamp(require_json_string(row.get("occurred_at", row.get("at")), "observed_at")))
        observed = matches[-1] if matches else None
        actual = observed.get("rating" if target == "feedback" else "value") if observed else None
        expected = prediction.get("expected_rating" if target == "feedback" else "expected_value")
        outcome = "unjudged" if actual is None or actual == "unknown" else "supported" if actual == expected else "contradicted"
        review = replace(event, kind="memory.reviewed", payload={"prediction_id": prediction.get("id"),
            "feedback_id": observed.get("id") if observed else "", "outcome": outcome})
        _review(connection, review)


def project_learning(connection: sqlite3.Connection, event: MemoryEvent) -> None:
    if event.kind == "memory.feedback":
        _feedback(connection, event)
    elif event.kind == "memory.predicted":
        _predict(connection, event)
    else:
        _review(connection, event)
