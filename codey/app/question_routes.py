"""User answers enter the same durable input queue as other conversation input."""
from __future__ import annotations

from typing import Any

from codey.app.input_queue_routes import bind_input_queue


def _deliver(ctx: Any, item: dict[str, Any]) -> dict[str, Any]:
    if item["state"] != "answer_accepted":
        return item
    identity, request = ctx.user_questions.answer_input(item)
    ctx.input_queue.create(identity, request)
    item = ctx.user_questions.queued(item["id"])
    ctx.emit({"type": "user_question_state", "session_id": item["session_id"], "question": item})
    return item


def recover_answers(ctx: Any) -> None:
    for item in ctx.user_questions.list():
        if item["state"] == "answer_accepted":
            _deliver(ctx, item)


def questions_response(ctx: Any, query: dict[str, list[str]]) -> tuple[int, dict[str, Any]]:
    bind_input_queue(ctx)
    identity = str((query.get("id") or [""])[0])
    if identity:
        item = ctx.user_questions.get(identity)
        if item is None:
            return 404, {"ok": False, "error": "question not found"}
        return 200, {"ok": True, "question": _deliver(ctx, item)}
    return 200, {"ok": True, "questions": [_deliver(ctx, item) for item in ctx.user_questions.list()]}


def question_answer_response(ctx: Any, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    bind_input_queue(ctx)
    identity = str(body.get("id") or "")
    revision = body.get("revision")
    if type(revision) is not int:
        return 400, {"ok": False, "error": "question revision required"}
    try:
        if body.get("action") == "cancel":
            item = ctx.user_questions.cancel(identity, revision)
        else:
            item = ctx.user_questions.answer(identity, revision, body.get("answer"))
            item = _deliver(ctx, item)
        ctx.input_queue.wake.set()
        ctx.emit({"type": "user_question_state", "session_id": item["session_id"], "question": item})
        return 202, {"ok": True, "question": item}
    except ValueError as exc:
        return 409, {"ok": False, "error": str(exc), "question": ctx.user_questions.get(identity)}
