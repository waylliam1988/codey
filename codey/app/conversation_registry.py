"""Durable conversation cache and persistence guard for app sessions."""

from __future__ import annotations

import threading
from collections.abc import Callable

from codey.agents.handoff import ConversationContext, ConversationSnapshot


class ConversationRegistry:
    def __init__(
        self,
        state_home: object | None = None,
        *,
        max_states: int,
        load_snapshot: Callable[[str], ConversationSnapshot | None] | None = None,
        save_snapshot: Callable[[str, ConversationSnapshot], None] | None = None,
    ) -> None:
        self.max_states = max(1, max_states)
        self._load_snapshot = load_snapshot
        self._save_snapshot = save_snapshot
        self.contexts: dict[str, ConversationContext] = {}
        self.lock = threading.Lock()
        self._forget_generations: dict[str, int] = {}

    def for_session(self, session_id: str) -> ConversationContext:
        with self.lock:
            context = self.contexts.pop(session_id, None)
            if context is not None:
                self.contexts[session_id] = context
                return context
            evicted = self._evict_oldest_if_needed()
            generation = self._forget_generations.get(session_id, 0)
        # DurableSession owns restartable conversation facts.  This registry
        # is deliberately only a bounded provider-window cache.
        if evicted is not None:
            _oldest_id, _oldest_context = evicted
        loaded = ConversationContext()
        if self._load_snapshot is not None:
            snapshot = self._load_snapshot(session_id)
            if snapshot is not None:
                loaded.snapshot = snapshot
        save_snapshot = self._save_snapshot
        if save_snapshot is not None:
            loaded.on_change = lambda value: save_snapshot(session_id, value.snapshot)
        with self.lock:
            if self._forget_generations.get(session_id, 0) != generation:
                loaded.on_change = None
                return loaded
            self.contexts[session_id] = loaded
            return loaded

    def forget(self, session_id: str) -> None:
        with self.lock:
            context = self.contexts.pop(session_id, None)
            if context is not None:
                context.on_change = None
            self._forget_generations[session_id] = self._forget_generations.get(session_id, 0) + 1

    def _evict_oldest_if_needed(self) -> tuple[str, ConversationContext] | None:
        """Detach the oldest entry; caller saves it outside ``self.lock``.

        Detach only severs in-memory links and tokens. Durable save happens
        in :meth:`for_session` after the lock is released so slow disk IO
        never stalls other sessions. The detached context stays detached
        (``on_change=None``) so late writes cannot repopulate the store.
        """
        if len(self.contexts) < self.max_states:
            return None
        oldest = next(iter(self.contexts))
        evicted = self.contexts.pop(oldest)
        evicted.on_change = None
        return (oldest, evicted)


__all__ = ["ConversationRegistry"]

