"""Built-in journaled memory. The host retains all executable authority."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from functools import partial

from codey.ghost.controls import policy
from codey.ghost.families import effective_family_state
from codey.ghost.projection import project_event
from codey.ghost.retrieval import relevance
from codey.ghost.storage import GhostDatabase, load, rows, scoped_rows
from codey.memory.calibration import prediction_calibration
from codey.memory.codec import (
    digest,
    item_from_payload,
    record_from_payload,
    record_payload,
    record_recency,
    timestamp,
    trace_for_item,
)
from codey.memory.contracts import (
    LEGACY_MEMORY_MAINTENANCE_POLICY_VERSION,
    MEMORY_MAINTENANCE_POLICY_VERSION,
    DeleteResult,
    DeleteScopeRequest,
    MemoryChange,
    MemoryConflict,
    MemoryControl,
    MemoryDecision,
    MemoryEvent,
    MemoryFamilyChange,
    MemoryFamilyState,
    MemoryFeedback,
    MemoryHost,
    MemoryItem,
    MemoryMaintenanceRequest,
    MemoryMaintenanceResult,
    MemoryPolicy,
    MemoryPrediction,
    MemoryProposal,
    MemoryQuery,
    MemoryRebuildResult,
    MemoryRecord,
    MemoryResult,
    MemoryRetentionPolicy,
    MemoryReview,
    MemoryTrace,
)
from codey.memory.slots import family_for
from codey.types.json_value import (
    JsonObject,
    JsonValue,
    require_json_integer,
    require_json_string,
    require_json_strings,
)

# Replay preserves decisions admitted under earlier validation rules.
_replay_event = partial(project_event, replaying=True)


def _candidate_filter(item: MemoryItem, state: MemoryFamilyState, query: MemoryQuery) -> str:
    if state != "enabled" and (not query.shadow or state in {"quarantined", "deleted", "demoted"}):
        return "family_" + state
    if item.kind == "preference" and query.slot_override is not None and item.slot == query.slot_override.slot:
        return "task_slot_override"
    return ""


class GhostMemory:
    def __init__(self, host: MemoryHost) -> None:
        self._host = host
        self.database = GhostDatabase(host)

    def records(self, scopes: tuple[str, ...]) -> tuple[MemoryRecord, ...]:
        with self.database.connect() as connection:
            found = scoped_rows(connection, "items", scopes)
        return tuple(record for raw in found if (record := record_from_payload(raw)).item.scope in scopes)

    def confirmation_available(self, ref: str) -> bool:
        with self.database.connect() as connection:
            decision = load(connection, "decisions", ref)
            return decision is not None and decision.get("accepted") is True and decision.get("scope") == "user"

    def retrieve(self, query: MemoryQuery) -> MemoryResult:
        if not query.scopes or query.max_items <= 0:
            return MemoryResult()
        now = timestamp(query.as_of)
        with self.database.connect() as connection:
            controls = policy(connection, ("user", *(query.control_scopes or query.scopes)))
            found = scoped_rows(connection, "items", query.scopes)
            states = {record.item.item_id: effective_family_state(connection, family_for(record.item), query.context or "chat",
                        ("user", *(query.control_scopes or query.scopes), record.item.scope))
                      for raw in found if (record := record_from_payload(raw)).item.scope in query.scopes}
            learned = {(require_json_string(row.get("item_id"), "item_id"), require_json_string(row.get("context", ""), "context")): row for row in scoped_rows(connection, "learning", query.scopes)}
        traces: list[MemoryTrace] = []
        candidates: list[tuple[float, MemoryRecord]] = []
        experiences = 0
        ordered = sorted(found, key=record_recency, reverse=True)
        for raw in ordered:
            self._host.check_cancelled()
            record = record_from_payload(raw)
            item = record.item
            if item.scope not in query.scopes:
                continue
            reason = ""
            if not controls.retrieve_projections:
                reason = "retrieval_disabled"
            elif record.state != "active":
                reason = record.state
            elif query.exclude_run_id and raw.get("run_id") == query.exclude_run_id:
                reason = "current_run"
            elif timestamp(item.created_at) > now:
                reason = "future_created"
            elif item.expires_at and timestamp(item.expires_at) <= now:
                reason = "expired"
            elif item.context and item.context != query.context:
                reason = "context_mismatch"
            elif filtered := _candidate_filter(item, states[item.item_id], query):
                reason = filtered
            elif item.kind == "experience":
                experiences += 1
                if experiences > 200:
                    reason = "experience_window"
            score = relevance(query.text, item, query.as_of)
            counts = learned.get((item.item_id, query.context))
            if counts is not None and counts.get("item_version") == digest(item.to_payload()):
                helpful = require_json_integer(counts.get("helpful"), "helpful")
                negative = require_json_integer(counts.get("negative"), "negative")
                score += 0.1 * (helpful - negative) / (helpful + negative + 2)
            if not reason and item.kind == "experience" and score <= 0:
                reason = "unrelated"
            if reason:
                traces.append(trace_for_item(item, reason, score))
            else:
                candidates.append((score, record))
        def rank(row: tuple[float, MemoryRecord]) -> tuple[float, str]:
            return -row[0], row[1].item.item_id

        candidates.sort(key=rank)
        reserved = next((row for row in candidates if row[1].item.kind == "preference" and not row[1].item.expires_at
                         and row[1].item.scope == "user"), None)
        if reserved is not None:
            candidates.remove(reserved)
            candidates.insert(0, reserved)
        selected = []
        for index, (score, record) in enumerate(candidates, 1):
            reason = "candidate" if index <= query.max_items else "result_limit"
            traces.append(trace_for_item(record.item, reason, score, index))
            if index <= query.max_items:
                selected.append(record.item)
        return MemoryResult(tuple(selected), tuple(replace(trace, family_state=states[trace.item_id], consumer=query.context) for trace in traces))

    def observe(self, event: MemoryEvent) -> None:
        self.database.append(event, project_event)

    def proposals(self, scope: str) -> tuple[MemoryProposal, ...]:
        with self.database.connect() as connection:
            found = scoped_rows(connection, "proposals", (scope,))
        return tuple(MemoryProposal(item.item_id, item, digest(item.to_payload()), self._relations(raw))
                     for raw in found if not raw.get("decision")
                     and (item := item_from_payload(raw.get("item"))).scope == scope)[:100]

    @staticmethod
    def _relations(raw: JsonObject) -> tuple[MemoryConflict, ...]:
        from codey.memory.contracts import MemoryRelation
        from codey.types.json_value import decode_json_object

        mapping: dict[str, MemoryRelation] = {"equivalent": "equivalent", "conflicting": "conflicting",
            "context_exception": "context_exception", "suspected_conflict": "suspected_conflict"}
        values = raw.get("relations", [])
        if not isinstance(values, list):
            raise ValueError("invalid memory relations")
        result = []
        for value in values:
            row = decode_json_object(value)
            relation = mapping.get(require_json_string(row.get("relation"), "relation"))
            if relation is None:
                raise ValueError("invalid memory relation")
            result.append(MemoryConflict(require_json_string(row.get("target_id"), "target_id"), relation,
                                          tuple(require_json_strings(row.get("source_refs"), "source_refs"))))
        return tuple(result)

    def _command(self, kind: str, identity: str, scope: str, at: str, payload: JsonObject,
                 provenance: tuple[str, ...]) -> None:
        self.observe(MemoryEvent(identity, kind, at, "", "", "", scope, payload, provenance))

    def family_change(self, change: MemoryFamilyChange) -> None:
        self._command("memory.family_changed", change.source_ref, change.scope, change.occurred_at,
            {"family": change.family, "consumer": change.consumer, "state": change.state,
             "evaluation_refs": list(change.evaluation_refs)}, (change.source_ref,))

    def maintain_batch(self, request: MemoryMaintenanceRequest) -> MemoryMaintenanceResult:
        payload: JsonObject = {"max_items": request.max_items, "cursor": request.cursor, "policy_version": request.policy_version}
        event = MemoryEvent(request.operation_id, "memory.maintenance_batch", request.as_of, "", "", "", "user",
                            payload, (request.operation_id,))
        legacy_retry = None
        if request.policy_version == MEMORY_MAINTENANCE_POLICY_VERSION:
            legacy_retry = replace(event, payload={**payload, "policy_version": LEGACY_MEMORY_MAINTENANCE_POLICY_VERSION})
        self.database.append(event, project_event, legacy_retry=legacy_retry)
        with self.database.connect() as connection:
            result = load(connection, "maintenance", request.operation_id)
        if result is None:
            raise ValueError("maintenance result is missing")
        completed = MemoryMaintenanceResult(require_json_integer(result.get("processed"), "processed"),
            require_json_string(result.get("cursor"), "cursor"), result.get("complete") is True)
        if completed.complete:
            self._retain_if_due(request.as_of)
        return completed

    def _retain_if_due(self, as_of: str) -> None:
        with self.database.connect() as connection:
            previous = load(connection, "retention", "policy")
            at = require_json_string(previous.get("as_of"), "as_of") if previous else ""
            if at and timestamp(as_of) - timestamp(at) < timedelta(days=1) and self.database.path.stat().st_size < 64 * 1024 * 1024:
                return
            if not policy(connection, ("user",)).run_maintenance or any(
                not policy(connection, record_from_payload(raw).control_scopes).run_maintenance for raw in rows(connection, "items")):
                return
        defaults = MemoryRetentionPolicy()
        selected = MemoryRetentionPolicy(*(require_json_integer(previous.get(key), key) for key in (
            "max_experiences_per_scope", "experience_days", "archived_days", "keep_events"))) if previous else defaults
        self.compact(selected, as_of=as_of)

    def compact(self, policy: MemoryRetentionPolicy, *, as_of: str) -> MemoryRebuildResult:
        floor = (timestamp(as_of) - timedelta(days=max(policy.experience_days, policy.archived_days))).isoformat()
        payload: JsonObject = {"max_experiences_per_scope": policy.max_experiences_per_scope,
            "experience_days": policy.experience_days, "archived_days": policy.archived_days, "keep_events": policy.keep_events}
        identity = "retention:" + digest({"at": as_of, **payload})
        self._command("memory.retained", identity, "user", as_of, payload, (identity,))
        return self.database.compact(as_of=floor, retired_before=as_of, keep_events=policy.keep_events, project=_replay_event)

    def observation_cursor(self) -> int:
        with self.database.connect() as connection:
            row = load(connection, "cursors", "core")
        return require_json_integer(row.get("sequence"), "sequence") if row else 0

    def maintenance_cursor(self) -> str:
        with self.database.connect() as connection:
            row = load(connection, "cursors", "maintenance")
        return require_json_string(row.get("cursor"), "cursor") if row else ""

    def decide(self, decision: MemoryDecision) -> None:
        if not decision.source_ref:
            raise ValueError("a memory decision requires a committed user decision reference")
        self._command("memory.decided", decision.source_ref, "user", decision.decided_at,
                      {"proposal_id": decision.proposal_id, "version": decision.version, "accepted": decision.accepted,
                       "supersedes": list(decision.supersedes)},
                      (decision.source_ref,))

    def delete_scope(self, request: DeleteScopeRequest) -> DeleteResult:
        at = request.requested_at or self._host.clock()
        payload: JsonObject = {"source_refs": list(request.source_refs)}
        identity = "delete:" + digest({"scope": request.scope, "at": at, **payload})
        self._command("memory.deleted", identity, request.scope, at, payload, (identity,))
        with self.database.connect() as connection:
            result = load(connection, "operations", identity)
        if result is None:
            raise ValueError("memory deletion has no committed result")
        return DeleteResult(require_json_integer(result.get("deleted"), "deleted"))

    def policy(self, scopes: tuple[str, ...]) -> MemoryPolicy:
        with self.database.connect() as connection:
            return policy(connection, ("user", *scopes))

    def set_control(self, control: MemoryControl) -> None:
        self._command("memory.controlled", control.source_ref, control.scope, control.occurred_at,
                      {"name": control.name, "enabled": control.enabled}, (control.source_ref,))

    def change(self, change: MemoryChange) -> None:
        self._command("memory.changed", change.source_ref, change.scope, change.occurred_at,
                      {"item_id": change.item_id, "state": change.state}, (change.source_ref,))

    def feedback(self, feedback: MemoryFeedback) -> None:
        payload: JsonObject = {"item_id": feedback.item_id, "run_id": feedback.run_id, "rating": feedback.rating,
                               "exposure_id": feedback.exposure_id, "behavior_id": feedback.behavior_id}
        if feedback.correction_text:
            payload.update(correction_text=feedback.correction_text, context_scope=feedback.context_scope)
        self._command("memory.feedback", feedback.feedback_id, feedback.scope, feedback.occurred_at, payload,
                      (feedback.source_ref,))

    def predict(self, prediction: MemoryPrediction) -> None:
        self._command("memory.predicted", prediction.prediction_id, prediction.scope, prediction.created_at,
                      {"item_id": prediction.item_id, "statement": prediction.statement,
                       "expected_rating": prediction.expected_rating, "due_at": prediction.due_at,
                       "target_kind": prediction.target_kind, "target_key": prediction.target_key,
                       "expected_value": prediction.expected_value, "workspace_revision": prediction.workspace_revision}, prediction.source_refs)

    def review(self, review: MemoryReview) -> None:
        self._command("memory.reviewed", review.source_ref, "user", review.occurred_at,
                      {"prediction_id": review.prediction_id, "feedback_id": review.feedback_id,
                       "outcome": review.outcome}, (review.source_ref,))

    def rebuild(self) -> MemoryRebuildResult:
        return self.database.rebuild(_replay_event)

    def export_state(self) -> dict[str, JsonValue]:
        with self.database.connect() as connection:
            found = rows(connection, "items")
            proposals = rows(connection, "proposals")
            controls, feedback, predictions, exposures, outcomes = (rows(connection, name) for name in
                ("controls", "feedback", "predictions", "exposures", "outcomes"))
            families, behaviors, learning, repairs, evaluations = (rows(connection, name) for name in ("families", "behaviors", "learning", "repairs", "evaluations"))
        records = [record_from_payload(row) for row in found]
        return {"enabled": True, "schema_version": 2,
                "items": [record.item.to_payload() for record in records if record.state == "active"],
                "records": [record_payload(record) for record in records],
                "proposals": [item_from_payload(row.get("item")).to_payload() for row in proposals],
                "controls": list(controls), "feedback": list(feedback), "predictions": list(predictions),
                "exposures": list(exposures), "outcomes": list(outcomes), "families": list(families),
                "behaviors": list(behaviors), "learning": list(learning), "repairs": list(repairs), "evaluations": list(evaluations),
                "calibration": prediction_calibration(predictions)}

    def maintain(self, *, as_of: str) -> None:
        timestamp(as_of)
        self._command("memory.maintained", "maintain:" + as_of, "user", as_of, {}, ("maintenance:" + as_of,))
