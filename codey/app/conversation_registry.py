"""Durable conversation cache and persistence guard for app sessions."""

from __future__ import annotations

import threading
from pathlib import Path

from codey.agents.handoff import ConversationContext
from codey.storage.conversation_store import ConversationStore


class ConversationRegistry:
    def __init__(
        self,
        state_home: str | Path | None = None,
        *,
        max_states: int,
    ) -> None:
        self.max_states = max(1, int(max_states))
        self.contexts: dict[str, ConversationContext] = {}
        self.tokens: dict[str, object] = {}
        self.lock = threading.Lock()
        self.store_lock = threading.Lock()
        self.store = ConversationStore(state_home) if state_home else ConversationStore()

    def for_session(self, session_id: str) -> ConversationContext:
        with self.lock:
            context = self.contexts.pop(session_id, None)
            if context is not None:
                self.contexts[session_id] = context
                return context
            evicted = self._evict_oldest_if_needed()
            token = object()
            with self.store_lock:
                self.tokens[session_id] = token
        # Store IO happens outside self.lock so one slow session load
        # cannot stall emit/subscribe or other sessions.
        if evicted is not None:
            oldest_id, oldest_context = evicted
            with self.store_lock:
                try:
                    self.store.save(oldest_id, oldest_context)
                except (OSError, ValueError):
                    pass
        loaded = self.store.load(session_id)
        loaded.on_change = (
            lambda value, owner=session_id, owner_token=token: self._save(
                owner,
                owner_token,
                value,
            )
        )
        with self.lock:
            with self.store_lock:
                if self.tokens.get(session_id) is not token:
                    existing = self.contexts.get(session_id)
                    if existing is not None:
                        return existing
                    # Superseded while loading (forget, eviction, or a newer
                    # load won the token): never reinstall the stale token or
                    # the stale load. Detach so late writes cannot repopulate
                    # the store, and let the caller use this run in memory.
                    loaded.on_change = None
                    return loaded
            self.contexts[session_id] = loaded
            return loaded

    def forget(self, session_id: str) -> None:
        with self.lock:
            context = self.contexts.pop(session_id, None)
            if context is not None:
                context.on_change = None
            with self.store_lock:
                self.tokens.pop(session_id, None)
                self.store.delete(session_id)

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
        with self.store_lock:
            self.tokens.pop(oldest, None)
        return (oldest, evicted)

    def _save(
        self,
        session_id: str,
        token: object,
        context: ConversationContext,
    ) -> None:
        with self.store_lock:
            if self.tokens.get(session_id) is not token:
                return
            try:
                self.store.save(session_id, context)
            except (OSError, ValueError):
                pass


__all__ = ["ConversationRegistry"]

