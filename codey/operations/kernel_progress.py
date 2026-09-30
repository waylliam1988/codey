"""Bounded deterministic progress observations for every kernel task kind."""
from __future__ import annotations

from collections import deque

from codey.agents.runaway_guard import attempt_record, should_block_or_remind
from codey.agents.state import SeenInfoLRU, seen_info_key


class KernelProgress:
    def __init__(self, stagnant_turns: int | None = None) -> None:
        self.limit = max(1, int(stagnant_turns)) if stagnant_turns is not None else 4
        self.seen = SeenInfoLRU()
        self.attempts = deque(maxlen=16)
        self.idle = 0

    def observe(self, results, session) -> bool:
        """Stop only after unchanged observations; successful mutations reset it."""
        progress = False
        epoch = max([0, *session.edited_files.values()])
        for result in results:
            call = result.call
            record = attempt_record(call, result, turn=session.turn, edit_epoch=epoch)
            self.attempts.append(record)
            changed = result.audit.get("changed") is True
            key = seen_info_key(call.name, str(call.args.get("path") or call.args.get("url") or
                                             call.args.get("id") or ""), result.model_text)
            novel = self.seen.add(key)
            if changed or (record.ok and novel):
                progress = True
        self.idle = 0 if progress else self.idle + 1
        guard = should_block_or_remind(self.attempts)
        # A newly changed file or genuinely new output must never be stopped
        # merely because its call arguments appeared earlier in the session.
        return not progress and (self.idle >= self.limit or guard.block)
