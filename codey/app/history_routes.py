"""Typed bounded task-visibility endpoint for browser history projections."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from codey.durable.session import DurableSession


class HistoryContext(Protocol):
    durable: DurableSession


def validate_runs_response(ctx: HistoryContext, body: Mapping[str, object]) -> tuple[int, dict[str, object]]:
    session = body.get("session_id")
    epoch = body.get("epoch")
    generation = body.get("path_generation")
    candidates = body.get("run_ids")
    if (not isinstance(session, str) or not session or not isinstance(epoch, str)
            or type(generation) is not int or generation < 0 or not isinstance(candidates, list)
            or len(candidates) > 200):
        return 400, {"ok": False, "error": "invalid history task validation request"}
    runs: list[str] = []
    for candidate in candidates:
        if not isinstance(candidate, str) or not candidate or len(candidate) > 256:
            return 400, {"ok": False, "error": "invalid history task identity"}
        runs.append(candidate)
    if not ctx.durable.has_conversation(session):
        if epoch != ctx.durable.database_id or generation != 0:
            return 409, {"ok": False, "error": "history snapshot changed"}
        return 200, {"schema_version": 1, "session_id": session, "epoch": epoch,
                     "path_generation": 0, "snapshot_revision": 0, "display_version": None,
                     "runs": [{"run_id": run, "visible": False} for run in dict.fromkeys(runs)]}
    try:
        result = ctx.durable.validate_history_runs(session, runs, epoch=epoch, generation=generation)
        return 200, result
    except ValueError as exc:
        return 409, {"ok": False, "error": str(exc)}
    except KeyError:
        return 404, {"ok": False, "error": "conversation_path_not_found"}
