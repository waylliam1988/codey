"""Conversation admission, suspended questions, and durable input ownership."""
from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from codey.app.input_queue import DurableConversationInputQueue
from codey.app.run_registry import RunSnapshot
from codey.durable.questions import DurableUserQuestionStore
from codey.utils.object_fields import require_object_fields

if TYPE_CHECKING:
    from codey.app.context_ports import RunContext
    from codey.durable.session import DurableSession


class ConversationRuntime:
    def __init__(self, root: Path, emit: Callable[[dict[str, object]], None], *, durable: DurableSession | None = None) -> None:
        if durable is None:
            raise ValueError("ConversationRuntime requires a durable session")
        self.inputs = DurableConversationInputQueue(durable, emit=emit)
        self.questions = DurableUserQuestionStore(durable)
        self.starts: dict[str, float] = {}
        self.admissions: dict[str, dict[str, object]] = {}

    def prepare(self, ctx: RunContext, payload: dict[str, object]) -> dict[str, object] | None:
        kind, run_id = payload.get("type"), str(payload.get("run_id") or "")
        if kind == "task_start" and run_id:
            self.starts.setdefault(run_id, time.monotonic())
            payload["started_at_ms"] = time.time_ns() // 1_000_000
            self.admissions[run_id] = {
                "intent": str(payload.get("intent") or "auto"),
                "max_turns": payload.get("max_turns", 50),
                "captured_auth": dict(require_object_fields(payload.get("entry_auth") or {})),
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
                selection_payload: object = selection.to_payload()
                continuation["captured_selection"] = require_object_fields(selection_payload)
            identity, session_id = payload["id"], payload["session_id"]
            if not isinstance(identity, str) or not isinstance(session_id, str):
                raise TypeError("question identity and session must be text")
            raw_record: object = self.questions.request(identity, session_id, run_id, payload["question"], continuation)
            record = require_object_fields(raw_record)
            if ctx.stop_flag_for(run_id).is_set() and record["state"] == "pending":
                revision = record["revision"]
                record_id = record["id"]
                if not isinstance(revision, int) or not isinstance(record_id, str):
                    raise TypeError("question revision must be an integer")
                cancelled: object = self.questions.cancel(record_id, revision)
                record = require_object_fields(cancelled)
            payload["question"] = record
        return payload

    def close(self, run: RunSnapshot | None) -> bool:
        if run and any(item["request"]["run_id"] == run.run_id and item["state"] in {"dispatching", "submitted", "unconfirmed"}
                       for item in self.inputs.list()):
            return False
        return self.inputs.close()
