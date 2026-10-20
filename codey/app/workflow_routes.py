"""Public metadata views and admission of historical workflow branches."""

from __future__ import annotations

import hashlib
from typing import Any

from codey.app.conversation_routes import register_fork
from codey.durable.errors import CommitConflict, IdempotencyConflict


def workflow_history_response(ctx: Any, query: dict[str, list[str]]) -> tuple[int, dict[str, Any]]:
    task_id = str((query.get("task_id") or [""])[0])
    try:
        after = int((query.get("after") or ["0"])[0])
        limit = int((query.get("limit") or ["100"])[0])
        if after < 0 or not 1 <= limit <= 256:
            raise ValueError("invalid workflow cursor or limit")
        ctx.durable.task(task_id)
        timeline = ctx.durable.task_timeline(task_id)
        points = tuple(point for point in timeline if point.commit_seq > after)
        unsettled: set[str] = set()
        forkable: dict[int, bool] = {}
        for point in timeline:
            for effect in point.effect_changes:
                if effect.settlement is None:
                    unsettled.add(effect.effect_id)
                else:
                    unsettled.discard(effect.effect_id)
            forkable[point.commit_seq] = bool(point.head_entry_id) and not unsettled
    except KeyError:
        return 404, {"ok": False, "error": "workflow_not_found"}
    except ValueError:
        return 400, {"ok": False, "error": "invalid_workflow_cursor"}
    page = points[:limit]
    return 200, {
        "ok": True, "task_id": task_id, "has_more": len(points) > limit,
        "after": page[-1].commit_seq if page else after,
        "items": [{
            "commit_seq": point.commit_seq, "revision": point.task.revision,
            "phase": point.task.phase, "status": str(point.task.status),
            "head_entry_id": point.head_entry_id,
            "forkable": forkable[point.commit_seq],
            "effect_changes": [{"effect_id": effect.effect_id, "status": effect.status,
                                "category": effect.category} for effect in point.effect_changes],
        } for point in page],
    }


def workflow_fork_response(ctx: Any, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    source = body.get("task_id")
    request_id = body.get("request_id")
    sequence = body.get("commit_seq")
    instruction = body.get("instruction")
    if (not isinstance(source, str) or not source or not isinstance(request_id, str) or not 1 <= len(request_id) <= 200
            or type(sequence) is not int or sequence < 0
            or not isinstance(instruction, str) or not 1 <= len(instruction.strip()) <= 8000):
        return 400, {"ok": False, "error": "invalid_workflow_fork"}
    identity = hashlib.sha256(f"{len(source)}:{source}{request_id}".encode()).hexdigest()[:32]
    conversation_id, task_id = "fork_" + identity, "run_" + identity
    try:
        result = ctx.durable.fork_task(source, sequence, conversation_id=conversation_id, task_id=task_id,
                                       request_id=request_id, instruction=instruction)
    except KeyError:
        return 404, {"ok": False, "error": "workflow_checkpoint_not_found"}
    except (CommitConflict, IdempotencyConflict) as exc:
        return 409, {"ok": False, "error": str(exc)}
    state = register_fork(ctx, ctx.durable.task(source).conversation_id, conversation_id)
    return 202, {"ok": True, "session_id": conversation_id, "run_id": task_id, "state": state,
                 "submission_id": result.submission_id, "commit_seq": result.sequence}
