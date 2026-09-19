from __future__ import annotations

import tempfile
import threading
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

    def test_session_load_runs_outside_registry_lock(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            registry = ConversationRegistry(td, max_states=4)
            real_load = registry.store.load
            seen_locked: list[bool] = []

            def _probing_load(session_id: str):
                seen_locked.append(registry.lock.locked())
                return real_load(session_id)

            registry.store.load = _probing_load  # type: ignore[method-assign]
            registry.for_session("chat-1")

        self.assertEqual(seen_locked, [False])

    def test_forget_during_load_never_resurrects(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            registry = ConversationRegistry(td, max_states=4)
            real_load = registry.store.load
            entered = threading.Event()
            release = threading.Event()
            results: list[object] = []

            def _blocking_load(session_id: str):
                entered.set()
                self.assertTrue(release.wait(timeout=5))
                return real_load(session_id)

            registry.store.load = _blocking_load  # type: ignore[method-assign]
            worker = threading.Thread(
                target=lambda: results.append(registry.for_session("s1")),
                daemon=True,
            )
            worker.start()
            self.assertTrue(entered.wait(timeout=5))
            registry.forget("s1")
            release.set()
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())

        self.assertEqual(len(results), 1)
        self.assertNotIn("s1", registry.contexts)
        self.assertNotIn("s1", registry.tokens)
        # Detached: late writes from the stale load must not repopulate.
        stale = results[0]
        self.assertIsNone(getattr(stale, "on_change", None))


if __name__ == "__main__":
    unittest.main()

