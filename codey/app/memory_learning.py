"""Explicit user feedback and predictions bound to memory and review windows."""

from __future__ import annotations

from typing import TYPE_CHECKING

from codey.memory.contracts import FeedbackRating, MemoryFeedback, MemoryPrediction, MemoryReview, PredictionOutcome
from codey.types.json_value import JsonObject, decode_json_object, require_json_string

if TYPE_CHECKING:
    from codey.app.context import AppContext


def learning_action(ctx: AppContext, payload: JsonObject, scope: str, ref: str, at: str) -> dict[str, object]:
    action = payload.get("action")
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
            ctx.memory.feedback(MemoryFeedback(ref, item_id, run_id, scope, rating, ref, at))
            return {"ok": True, "feedback_id": ref}
        statement = require_json_string(payload.get("statement"), "statement")
        due = require_json_string(payload.get("due_at"), "due_at")
        ctx.memory.predict(MemoryPrediction(ref, item_id, scope, statement, rating, (ref,), at, due))
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
