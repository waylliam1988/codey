"""Accept a question answer and wake its original durable task."""
from __future__ import annotations

from typing import Any


def questions_response(ctx: Any, query: dict[str, list[str]]) -> tuple[int, dict[str, Any]]:
    identity = str((query.get("id") or [""])[0])
    if identity:
        item = ctx.user_questions.get(identity)
        if item is None:
            return 404, {"ok": False, "error": "question not found"}
        return 200, {"ok": True, "question": item}
    return 200, {"ok": True, "questions": ctx.user_questions.list()}


def question_answer_response(ctx: Any, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    identity = str(body.get("id") or "")
    revision = body.get("revision")
    if type(revision) is not int:
        return 400, {"ok": False, "error": "question revision required"}
    try:
        if body.get("action") == "cancel":
            item = ctx.user_questions.cancel(identity, revision)
        else:
            item = ctx.user_questions.answer(identity, revision, body.get("answer"))
        ctx.emit({"type": "user_question_state", "session_id": item["session_id"], "question": item})
        return 202, {"ok": True, "question": item}
    except ValueError as exc:
        return 409, {"ok": False, "error": str(exc), "question": ctx.user_questions.get(identity)}
