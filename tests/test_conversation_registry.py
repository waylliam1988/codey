from __future__ import annotations

import tempfile
import unittest

from codey.agents.handoff import ConversationSnapshot
from codey.app.conversation_registry import ConversationRegistry


class ConversationRegistryTests(unittest.TestCase):
    def test_persists_and_restores_session_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            first = ConversationRegistry(td, max_states=4)
            context = first.for_session("chat-1")
            context.update_snapshot(ConversationSnapshot(mode="chat", goal="remember me"))

            second = ConversationRegistry(td, max_states=4)
            restored = second.for_session("chat-1")

        self.assertEqual(restored.snapshot.goal, "remember me")

    def test_lru_eviction_detaches_old_context_and_token(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            registry = ConversationRegistry(td, max_states=2)
            old = registry.for_session("chat-1")
            registry.for_session("chat-2")
            registry.for_session("chat-3")

            old.update_snapshot(ConversationSnapshot(mode="chat", goal="stale"))
            fresh = registry.for_session("chat-1")

        self.assertEqual(set(registry.contexts), {"chat-3", "chat-1"})
        self.assertEqual(fresh.snapshot.goal, "")

    def test_forget_removes_store_and_detaches_callback(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            registry = ConversationRegistry(td, max_states=4)
            context = registry.for_session("chat-1")
            context.update_snapshot(ConversationSnapshot(mode="chat", goal="old"))
            path = registry.store.path_for("chat-1")

            registry.forget("chat-1")
            context.update_snapshot(ConversationSnapshot(mode="chat", goal="stale"))

            fresh = registry.for_session("chat-1")

        self.assertFalse(path.exists())
        self.assertEqual(fresh.snapshot.goal, "")


if __name__ == "__main__":
    unittest.main()

