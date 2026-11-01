"""Bounded deterministic progress observations for every kernel task kind."""
from __future__ import annotations

from collections import deque
from typing import Any

from codey.agents.runaway_guard import attempt_record, should_block_or_remind
from codey.agents.state import SeenInfoLRU, ToolAttemptRecord, seen_info_key
from codey.runtime.core.models import ToolCall, ToolResult
from codey.runtime.core.progress import ProgressSnapshot


class KernelProgress:
    def __init__(self, stagnant_turns: int | None = None, *, snapshot: ProgressSnapshot | None = None) -> None:
        requested_limit = max(1, stagnant_turns) if stagnant_turns is not None else 4
        snapshot = snapshot or ProgressSnapshot(limit=requested_limit)
        if snapshot.last_batch_id and requested_limit != snapshot.limit:
            raise ValueError("progress budget differs from the admitted operation")
        self.limit = snapshot.limit if snapshot.last_batch_id else requested_limit
        self.seen = SeenInfoLRU()
        for key in snapshot.seen:
            self.seen.add(key)
        self.attempts: deque[ToolAttemptRecord] = deque(snapshot.attempts, maxlen=16)
        self.idle = snapshot.idle
        self.last_batch_id = snapshot.last_batch_id
        self.schema_version = snapshot.schema_version
        self.repair_failure_key = snapshot.repair_failure_key
        self.repair_rejections = snapshot.repair_rejections

    def snapshot(self) -> ProgressSnapshot:
        return ProgressSnapshot(self.limit, self.seen.keys(), tuple(self.attempts), self.idle, self.last_batch_id,
                                self.schema_version, self.repair_failure_key, self.repair_rejections)

    @property
    def stopped(self) -> bool:
        return (self.repair_rejections >= self.limit
                or self.idle > 0 and (self.idle >= self.limit or should_block_or_remind(self.attempts).block))

    def repeat_feedback(self) -> str:
        """Give the model a correction before the existing repeat limit wins."""
        if not self.idle or self.stopped or len(self.attempts) < 2:
            return ""
        previous, latest = list(self.attempts)[-2:]
        if (previous.call_fp != latest.call_fp or previous.result_fp != latest.result_fp
                or previous.changed or latest.changed or previous.edit_epoch != latest.edit_epoch):
            return ""
        status = "already succeeded" if latest.ok else "already failed"
        reminder = (f"The {latest.tool} call {status}; you repeated the same arguments and received the same result. "
                "Do not call it again with identical arguments. Continue from the recorded result with the next "
                "required action, change the request to address the observed problem, or explain the specific blocker.")
        if latest.tool == "research_plan":
            reminder += " Recording a plan is not completed investigation. Repeating it is not progress."
        return reminder

    def observe(self, results: Any, session: Any, *, batch_id: str = "") -> bool:
        """Stop only after unchanged observations; successful mutations reset it.

        Progress accounting never crashes the loop: a telemetry fault for one
        result counts as no-progress for that result (safe direction: the
        task may stop earlier, never spins forever).
        """
        if batch_id and batch_id == self.last_batch_id:
            return self.stopped
        progress = False
        epoch = max([0, *session.edited_files.values()])
        for result in results:
            call = result.call
            try:
                record = attempt_record(call, result, turn=session.turn, edit_epoch=epoch)
            except Exception:
                continue
            self.attempts.append(record)
            changed = result.audit.get("changed") is True
            key = seen_info_key(call.name, str(call.args.get("path") or call.args.get("url") or
                                             call.args.get("id") or ""), result.model_text)
            novel = self.seen.add(key)
            if changed or (record.ok and novel):
                progress = True
        self.idle = 0 if progress else self.idle + 1
        self.last_batch_id = batch_id
        # A newly changed file or genuinely new output must never be stopped
        # merely because its call arguments appeared earlier in the session.
        return self.stopped

    def reject_completion(self, args: dict[str, Any], feedback: str, session: Any, *, failure_key: str = "") -> None:
        """Account for a rejected proposal without inventing a tool execution.

        Unkeyed proposals retain ordinary exact-repeat/cycle accounting. A
        keyed domain issue has a durable rejection budget independent of tool
        history. Only a different validation issue starts a new repair episode;
        reads, paraphrases and replacement selections do not reset its count.
        Persist this snapshot atomically with the control decision. It does not
        claim a result batch: last_batch_id retains its original tool receipt.
        """
        call = ToolCall("done", args)
        result = ToolResult(call, failure_key or feedback, ok=False)
        record = attempt_record(call, result, turn=session.turn, edit_epoch=max([0, *session.edited_files.values()]))
        repeats = sum(previous.tool == "done" and previous.result_fp == record.result_fp for previous in self.attempts)
        self.attempts.append(record)
        novel = self.seen.add(seen_info_key("done", record.call_fp, feedback))
        self.idle = 0 if novel else self.idle + 1
        if failure_key:
            previous = self.repair_rejections if self.repair_failure_key == failure_key else (
                repeats if self.schema_version == 1 else 0)
            self.repair_failure_key = failure_key
            self.repair_rejections = min(self.limit, previous + 1)
            self.schema_version = 2
            self.idle = max(self.idle, self.repair_rejections)
        else:
            self.repair_failure_key = ""
            self.repair_rejections = 0
