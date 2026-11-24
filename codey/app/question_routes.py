"""Accept a question answer and wake its original durable task."""
from __future__ import annotations

from typing import TYPE_CHECKING

from codey.utils.object_fields import require_object_fields, require_object_rows

if TYPE_CHECKING:
    from codey.app.context import AppContext


def questions_response(ctx: AppContext, query: dict[str, list[str]]) -> tuple[int, dict[str, object]]:
    identity = (query.get("id") or [""])[0]
    if identity:
        item: object = ctx.user_questions.get(identity)
        if item is None:
            return 404, {"ok": False, "error": "question not found"}
        return 200, {"ok": True, "question": require_object_fields(item)}
    rows: object = ctx.user_questions.list()
    return 200, {"ok": True, "questions": require_object_rows(rows)}


def question_answer_response(ctx: AppContext, body: dict[str, object]) -> tuple[int, dict[str, object]]:
    identity = str(body.get("id") or "")
    revision = body.get("revision")
    if type(revision) is not int:
        return 400, {"ok": False, "error": "question revision required"}
    action = body.get("action", "answer")
    if action != "answer" and action != "cancel":
        return 400, {"ok": False, "error": "invalid question action"}
    try:
        item: dict[str, object]
        if action == "cancel":
            item = ctx.user_questions.cancel(identity, revision)
        else:
            answer = body.get("answer")
            # Keep answer validation and conflict ordering in the durable store.
            item = ctx.user_questions.answer(identity, revision, answer if isinstance(answer, str) else "")
        question = require_object_fields(item)
        ctx.emit({"type": "user_question_state", "session_id": question["session_id"], "question": question})
        return 202, {"ok": True, "question": question}
    except ValueError as exc:
        current: object = ctx.user_questions.get(identity)
        return 409, {"ok": False, "error": str(exc), "question": (
            require_object_fields(current) if current is not None else None
        )}
