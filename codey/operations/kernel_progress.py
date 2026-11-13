"""Bounded deterministic progress observations for every kernel task kind."""
from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from typing import Protocol

from codey.agents.runaway_guard import attempt_record, should_block_or_remind
from codey.agents.state import SeenInfoLRU, ToolAttemptRecord, seen_info_key
from codey.operations.kernel_protocol import TurnSnapshot
from codey.operations.progress_comparison import EFFECT_OBSERVATIONS, progress_comparison
from codey.runtime.core.models import ToolCall, ToolResult
from codey.runtime.core.progress import ProgressSnapshot
from codey.types.json_value import JsonObject


class ProgressSession(Protocol):
    @property
    def turn(self) -> int: ...

    @property
    def edited_files(self) -> Mapping[str, int]: ...

    @property
    def _memory_results(self) -> Mapping[str, ToolResult]: ...


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
        if self.repair_rejections >= self.limit:
            return True
        safe: list[ToolAttemptRecord] = []
        for record in reversed(self.attempts):
            if record.tool in EFFECT_OBSERVATIONS:
                break
            safe.insert(0, record)
        # Once an effect has left the bounded history, all retained attempts
        # are observations again. Large admitted budgets still use full idle.
        idle = min(self.idle, len(safe)) if len(safe) < len(self.attempts) else self.idle
        return idle > 0 and (idle >= self.limit or should_block_or_remind(safe).block)

    def repeat_feedback(self) -> str:
        """Give the model a correction before the existing repeat limit wins."""
        if not self.idle or self.stopped or len(self.attempts) < 2:
            return ""
        previous, latest = list(self.attempts)[-2:]
        # Read-only repeats already stop above. A detected cycle that remains
        # runnable contains an effect and needs a reminder rather than a stop.
        effect_cycle = should_block_or_remind(self.attempts).block
        if not effect_cycle and (previous.call_fp != latest.call_fp or previous.result_fp != latest.result_fp
                or previous.changed or latest.changed or previous.edit_epoch != latest.edit_epoch):
            return ""
        status = "already succeeded" if latest.ok else "already failed"
        if latest.tool in EFFECT_OBSERVATIONS or effect_cycle:
            return ("Repeated browser operations have not provided new visible information. This does not establish "
                    "whether an effect occurred. Observe the current page before deciding another effect; use an "
                    "available snapshot or read action. Continue with outstanding requirements when the recorded "
                    "observations suffice. Repeat an effect only when the task and current observations justify it.")
        reminder = (f"The {latest.tool} call {status}; you repeated the same arguments and received the same result. "
                "Do not call it again with identical arguments. Continue from the recorded result with the next "
                "required action, change the request to address the observed problem, or explain the specific blocker.")
        if latest.tool == "research_plan":
            reminder += " Recording a plan is not completed investigation. Repeating it is not progress."
        return reminder

    def observe(self, results: Sequence[ToolResult], session: ProgressSession, *, batch_id: str = "",
                snapshot: TurnSnapshot | None = None) -> bool:
        """Compare observations without treating repeated effects as safe to stop.

        Progress accounting never crashes the loop: a telemetry fault for one
        result counts as no-progress for that result (safe direction: the
        task may stop earlier, never spins forever).
        """
        if batch_id and batch_id == self.last_batch_id:
            return self.stopped
        progress = False
        epoch = max([0, *session.edited_files.values()])
        for result in results:
            try:
                comparison = progress_comparison(result, session._memory_results, snapshot)
                call = comparison.call
                record = attempt_record(call, comparison.result, turn=session.turn, edit_epoch=epoch)
            except Exception:
                continue
            self.attempts.append(record)
            changed = result.audit.get("changed") is True
            key = seen_info_key(call.name, str(call.args.get("path") or call.args.get("url") or
                                             call.args.get("id") or ""), comparison.information)
            novel = self.seen.add(key)
            prior = next((item for item in reversed(list(self.attempts)[:-1]) if item.call_fp == record.call_fp), None)
            observed_transition = (call.name.startswith("browser_") and prior is not None
                                   and prior.result_fp != record.result_fp)
            if changed or (record.ok and (novel or observed_transition)):
                progress = True
        self.idle = 0 if progress else self.idle + 1
        self.last_batch_id = batch_id
        # A newly changed file or genuinely new output must never be stopped
        # merely because its call arguments appeared earlier in the session.
        return self.stopped

    def reject_completion(self, args: JsonObject, feedback: str, session: ProgressSession, *, failure_key: str = "") -> None:
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
