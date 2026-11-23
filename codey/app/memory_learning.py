"""Explicit user feedback and predictions bound to memory and review windows."""

from __future__ import annotations

from typing import TYPE_CHECKING

from codey.memory.contracts import FeedbackRating, MemoryFeedback, MemoryPrediction, MemoryReview, PredictionOutcome
from codey.types.json_value import JsonObject, decode_json_object, require_json_string
from codey.workspace.context_admission import request_scope

if TYPE_CHECKING:
    from codey.app.context import AppContext


def learning_action(ctx: AppContext, payload: JsonObject, scope: str, ref: str, at: str) -> dict[str, object]:
    action = payload.get("action")
    if action == "repair":
        from codey.memory.contracts import MemoryEvent

        fields: JsonObject = {key: payload[key] for key in ("repair_id", "failure_id", "action_ids", "verification_id", "statement", "workspace_revision", "regression_id") if key in payload}
        if not ctx.memory.observe(MemoryEvent(ref, "memory.repair_reviewed", at, "", "", "", scope, fields, (ref,))):
            raise ValueError("repair attribution was not accepted")
        return {"ok": True}
    ratings: dict[str, FeedbackRating] = {"helpful": "helpful", "unhelpful": "unhelpful", "correction": "correction"}
    if action in {"feedback", "predict"}:
        rating = ratings.get(require_json_string(payload.get("rating", "helpful"), "rating"))
        if rating is None:
            raise ValueError("rating must be helpful, unhelpful or correction")
        item_id = require_json_string(payload.get("item_id"), "item_id")
        if action == "feedback":
            run_id = require_json_string(payload.get("run_id"), "run_id")
            task = ctx.durable.task(run_id)
            if task.phase != "completed":
                raise ValueError("feedback requires a completed, delivered run")
            correction = require_json_string(payload.get("correction_text", ""), "correction_text")
            if correction and (rating != "correction" or not correction.strip() or len(correction) > 1200):
                raise ValueError("correction text requires a nonempty correction of at most 1200 characters")
            session_id = require_json_string(payload.get("session_id", ""), "session_id")
            project = task.state.get("project")
            context_scope = (request_scope(project, task.conversation_id) if correction and isinstance(project, str)
                             and project and task.conversation_id == session_id else "")
            ctx.memory.feedback(MemoryFeedback(ref, item_id, run_id, scope, rating, ref, at,
                require_json_string(payload.get("exposure_id", ""), "exposure_id"),
                require_json_string(payload.get("behavior_id", ""), "behavior_id"), correction, context_scope))
            return {"ok": True, "feedback_id": ref}
        statement = require_json_string(payload.get("statement"), "statement")
        due = require_json_string(payload.get("due_at"), "due_at")
        target = require_json_string(payload.get("target_kind", "feedback"), "target_kind")
        if target not in {"feedback", "behavior", "verification"}:
            raise ValueError("unknown prediction target")
        ctx.memory.predict(MemoryPrediction(ref, item_id, scope, statement, rating, (ref,), at, due,
            "feedback" if target == "feedback" else "behavior" if target == "behavior" else "verification",
            require_json_string(payload.get("target_key", ""), "target_key"),
            require_json_string(payload.get("expected_value", ""), "expected_value"),
            require_json_string(payload.get("workspace_revision", ""), "workspace_revision")))
        return {"ok": True, "prediction_id": ref}
    if action == "review":
        identity = require_json_string(payload.get("prediction_id"), "prediction_id")
        feedback_id = require_json_string(payload.get("feedback_id", ""), "feedback_id")
        state = ctx.memory.export_state()
        predictions = state.get("predictions", [])
        prediction = next((decode_json_object(row) for row in predictions
                           if isinstance(row, dict) and row.get("id") == identity), None) if isinstance(predictions, list) else None
        if prediction is None or prediction.get("scope") != scope:
            raise ValueError("prediction is outside the selected scope")
        outcome: PredictionOutcome = "unjudged"
        if feedback_id:
            feedback = state.get("feedback", [])
            observed = next((decode_json_object(row) for row in feedback
                             if isinstance(row, dict) and row.get("id") == feedback_id), None) if isinstance(feedback, list) else None
            if observed is None:
                raise ValueError("feedback does not exist")
            outcome = "supported" if observed.get("rating") == prediction.get("expected_rating") else "contradicted"
        ctx.memory.review(MemoryReview(identity, feedback_id, outcome, ref, at))
        return {"ok": True, "outcome": outcome}
    raise ValueError("unsupported learning command")
