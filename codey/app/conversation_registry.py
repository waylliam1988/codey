"""Durable conversation cache and persistence guard for app sessions."""

from __future__ import annotations

from pathlib import Path
import threading

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
            if context is None:
                self._evict_oldest_if_needed()
                with self.store_lock:
                    token = object()
                    self.tokens[session_id] = token
                    context = self.store.load(session_id)
                context.on_change = (
                    lambda value, owner=session_id, owner_token=token: self._save(
                        owner,
                        owner_token,
                        value,
                    )
                )
            self.contexts[session_id] = context
            return context

    def forget(self, session_id: str) -> None:
        with self.lock:
            context = self.contexts.pop(session_id, None)
            if context is not None:
                context.on_change = None
            with self.store_lock:
                self.tokens.pop(session_id, None)
                self.store.delete(session_id)

    def _evict_oldest_if_needed(self) -> None:
        if len(self.contexts) < self.max_states:
            return
        oldest = next(iter(self.contexts))
        evicted = self.contexts.pop(oldest)
        evicted.on_change = None
        with self.store_lock:
            self.tokens.pop(oldest, None)

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

