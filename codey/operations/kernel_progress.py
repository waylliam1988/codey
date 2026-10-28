"""Bounded deterministic progress observations for every kernel task kind."""
from __future__ import annotations

from collections import deque
from typing import Any

from codey.agents.runaway_guard import attempt_record, should_block_or_remind
from codey.agents.state import SeenInfoLRU, ToolAttemptRecord, seen_info_key
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

    def snapshot(self) -> ProgressSnapshot:
        return ProgressSnapshot(self.limit, self.seen.keys(), tuple(self.attempts), self.idle, self.last_batch_id)

    @property
    def stopped(self) -> bool:
        return self.idle > 0 and (self.idle >= self.limit or should_block_or_remind(self.attempts).block)

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
