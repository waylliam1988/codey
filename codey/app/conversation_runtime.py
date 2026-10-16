"""Conversation admission, suspended questions, and durable input ownership."""
from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from codey.app.input_queue import ConversationInputQueue
from codey.app.run_registry import RunSnapshot
from codey.app.user_questions import UserQuestionStore


class ConversationRuntime:
    def __init__(self, root: Path, emit: Callable[[dict[str, Any]], None]) -> None:
        self.inputs = ConversationInputQueue(root, emit=emit)
        self.questions = UserQuestionStore(root)
        self.starts: dict[str, float] = {}
        self.admissions: dict[str, dict[str, Any]] = {}

    def prepare(self, ctx: Any, payload: dict[str, Any]) -> dict[str, Any] | None:
        kind, run_id = payload.get("type"), str(payload.get("run_id") or "")
        if kind == "task_start" and run_id:
            self.starts.setdefault(run_id, time.monotonic())
            self.admissions[run_id] = {
                "intent": str(payload.get("intent") or "auto"),
                "max_turns": payload.get("max_turns", 50),
                "captured_auth": dict(payload.get("entry_auth") or {}),
            }
        elif kind == "task_done":
            started = self.starts.pop(run_id, None)
            self.admissions.pop(run_id, None)
            if started is not None:
                payload["elapsed_ms"] = max(0, round((time.monotonic() - started) * 1000))
        elif kind == "user_question":
            run = ctx.active_run_for(session_id=str(payload.get("session_id") or ""))
            if run is None or run.run_id != run_id:
                return None
            continuation = {**self.admissions.get(run_id, {}), "project": run.project or "", "provider": run.provider_id}
            selection = ctx.run_registry.api_selection_for(run_id)
            if selection is not None:
                continuation["captured_selection"] = selection.to_payload()
            record = self.questions.request(payload["id"], payload["session_id"], run_id, payload["question"], continuation)
            if ctx.stop_flag_for(run_id).is_set() and record["state"] == "pending":
                record = self.questions.cancel(record["id"], record["revision"])
            payload["question"] = record
        return payload

    def close(self, run: RunSnapshot | None) -> bool:
        if run and any(item["request"]["run_id"] == run.run_id and item["state"] in {"dispatching", "submitted", "unconfirmed"}
                       for item in self.inputs.list()):
            return False
        return self.inputs.close()
