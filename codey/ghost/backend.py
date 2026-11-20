"""Built-in journaled memory. The host retains all executable authority."""

from __future__ import annotations

from codey.ghost.projection import policy, project_event, record_from_payload, record_payload
from codey.ghost.retrieval import relevance
from codey.ghost.storage import GhostDatabase, load, rows
from codey.memory.codec import digest, item_from_payload, timestamp, trace_for_item
from codey.memory.contracts import (
    DeleteResult,
    DeleteScopeRequest,
    MemoryChange,
    MemoryControl,
    MemoryDecision,
    MemoryEvent,
    MemoryFeedback,
    MemoryHost,
    MemoryPolicy,
    MemoryPrediction,
    MemoryProposal,
    MemoryQuery,
    MemoryRebuildResult,
    MemoryRecord,
    MemoryResult,
    MemoryReview,
    MemoryTrace,
)
from codey.types.json_value import JsonObject, JsonValue, require_json_integer


class GhostMemory:
    def __init__(self, host: MemoryHost) -> None:
        self._host = host
        self.database = GhostDatabase(host)

    def records(self, scopes: tuple[str, ...]) -> tuple[MemoryRecord, ...]:
        with self.database.connect() as connection:
            found = rows(connection, "items")
        return tuple(record for raw in found if (record := record_from_payload(raw)).item.scope in scopes)

    def retrieve(self, query: MemoryQuery) -> MemoryResult:
        if not query.scopes or query.max_items <= 0:
            return MemoryResult()
        now = timestamp(query.as_of)
        with self.database.connect() as connection:
            controls = policy(connection, ("user", *(query.control_scopes or query.scopes)))
            found = rows(connection, "items")
        traces: list[MemoryTrace] = []
        candidates: list[tuple[float, MemoryRecord]] = []
        experiences = 0
        for raw in reversed(found):
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
            elif item.kind == "experience":
                experiences += 1
                if experiences > 200:
                    reason = "experience_window"
            score = relevance(query.text, item, query.as_of)
            if not reason and item.kind == "experience" and score <= 0:
                reason = "unrelated"
            if reason:
                traces.append(trace_for_item(item, reason, score))
            else:
                candidates.append((score, record))
        def rank(row: tuple[float, MemoryRecord]) -> tuple[float, str]:
            return -row[0], row[1].item.item_id

        candidates.sort(key=rank)
        selected = []
        for index, (score, record) in enumerate(candidates, 1):
            reason = "candidate" if index <= query.max_items else "result_limit"
            traces.append(trace_for_item(record.item, reason, score, index))
            if index <= query.max_items:
                selected.append(record.item)
        return MemoryResult(tuple(selected), tuple(traces))

    def observe(self, event: MemoryEvent) -> None:
        self.database.append(event, project_event)

    def proposals(self, scope: str) -> tuple[MemoryProposal, ...]:
        with self.database.connect() as connection:
            found = rows(connection, "proposals")
        return tuple(MemoryProposal(item.item_id, item, digest(item.to_payload()))
                     for raw in found if not raw.get("decision")
                     and (item := item_from_payload(raw.get("item"))).scope == scope)[:100]

    def _command(self, kind: str, identity: str, scope: str, at: str, payload: JsonObject,
                 provenance: tuple[str, ...]) -> None:
        self.observe(MemoryEvent(identity, kind, at, "", "", "", scope, payload, provenance))

    def decide(self, decision: MemoryDecision) -> None:
        if not decision.source_ref:
            raise ValueError("a memory decision requires a committed user decision reference")
        self._command("memory.decided", decision.source_ref, "user", decision.decided_at,
                      {"proposal_id": decision.proposal_id, "version": decision.version, "accepted": decision.accepted},
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
        self._command("memory.feedback", feedback.feedback_id, feedback.scope, feedback.occurred_at,
                      {"item_id": feedback.item_id, "run_id": feedback.run_id, "rating": feedback.rating},
                      (feedback.source_ref,))

    def predict(self, prediction: MemoryPrediction) -> None:
        self._command("memory.predicted", prediction.prediction_id, prediction.scope, prediction.created_at,
                      {"item_id": prediction.item_id, "statement": prediction.statement,
                       "expected_rating": prediction.expected_rating, "due_at": prediction.due_at}, prediction.source_refs)

    def review(self, review: MemoryReview) -> None:
        self._command("memory.reviewed", review.source_ref, "user", review.occurred_at,
                      {"prediction_id": review.prediction_id, "feedback_id": review.feedback_id,
                       "outcome": review.outcome}, (review.source_ref,))

    def rebuild(self) -> MemoryRebuildResult:
        return self.database.rebuild(project_event)

    def export_state(self) -> dict[str, JsonValue]:
        with self.database.connect() as connection:
            found = rows(connection, "items")
            proposals = rows(connection, "proposals")
            controls, feedback, predictions, exposures, outcomes = (rows(connection, name) for name in
                ("controls", "feedback", "predictions", "exposures", "outcomes"))
        records = [record_from_payload(row) for row in found]
        return {"enabled": True, "schema_version": 2,
                "items": [record.item.to_payload() for record in records if record.state == "active"],
                "records": [record_payload(record) for record in records],
                "proposals": [item_from_payload(row.get("item")).to_payload() for row in proposals],
                "controls": list(controls), "feedback": list(feedback), "predictions": list(predictions),
                "exposures": list(exposures), "outcomes": list(outcomes)}

    def maintain(self, *, as_of: str) -> None:
        timestamp(as_of)
        self._command("memory.maintained", "maintain:" + as_of, "user", as_of, {}, ("maintenance:" + as_of,))
