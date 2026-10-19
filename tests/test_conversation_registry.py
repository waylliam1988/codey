from __future__ import annotations

import threading
import unittest

from codey.agents.handoff import ConversationSnapshot
from codey.app.conversation_registry import ConversationRegistry


def _registry(snapshots: dict[str, ConversationSnapshot], *, max_states: int = 4, load=None, save=None) -> ConversationRegistry:
    return ConversationRegistry(
        None,
        max_states=max_states,
        load_snapshot=load or (lambda session_id: snapshots.get(session_id)),
        save_snapshot=save or (lambda session_id, snapshot: snapshots.__setitem__(session_id, snapshot)),
    )


class ConversationRegistryTests(unittest.TestCase):
    def test_persists_and_restores_session_snapshot(self) -> None:
        snapshots: dict[str, ConversationSnapshot] = {}
        first = _registry(snapshots)
        first.for_session("chat-1").update_snapshot(ConversationSnapshot(mode="chat", goal="remember me"))
        restored = _registry(snapshots).for_session("chat-1")
        self.assertEqual(restored.snapshot.goal, "remember me")

    def test_lru_eviction_detaches_old_context_and_token(self) -> None:
        registry = _registry({}, max_states=2)
        old = registry.for_session("chat-1")
        registry.for_session("chat-2")
        registry.for_session("chat-3")
        old.update_snapshot(ConversationSnapshot(mode="chat", goal="stale"))
        fresh = registry.for_session("chat-1")
        self.assertEqual(set(registry.contexts), {"chat-3", "chat-1"})
        self.assertEqual(fresh.snapshot.goal, "")

    def test_eviction_saves_last_state_and_stale_writes_do_not_overwrite(self) -> None:
        snapshots: dict[str, ConversationSnapshot] = {}
        registry = _registry(snapshots, max_states=2)
        old = registry.for_session("chat-1")
        old.update_snapshot(ConversationSnapshot(mode="chat", goal="keep me"))
        registry.for_session("chat-2")
        registry.for_session("chat-3")
        self.assertIsNone(old.on_change)
        old.update_snapshot(ConversationSnapshot(mode="chat", goal="stale"))
        self.assertEqual(registry.for_session("chat-1").snapshot.goal, "keep me")

    def test_evict_save_runs_outside_registry_lock(self) -> None:
        snapshots: dict[str, ConversationSnapshot] = {}
        registry: ConversationRegistry
        seen_locked: list[bool] = []

        def save(session_id: str, snapshot: ConversationSnapshot) -> None:
            seen_locked.append(registry.lock.locked())
            snapshots[session_id] = snapshot

        registry = _registry(snapshots, max_states=1, save=save)
        registry.for_session("chat-1").update_snapshot(ConversationSnapshot(mode="chat", goal="keep me"))
        registry.for_session("chat-2")
        self.assertEqual(seen_locked, [False])

    def test_forget_detaches_callback_and_forgets_cached_context(self) -> None:
        snapshots: dict[str, ConversationSnapshot] = {}
        registry = _registry(snapshots)
        context = registry.for_session("chat-1")
        context.update_snapshot(ConversationSnapshot(mode="chat", goal="old"))
        registry.forget("chat-1")
        context.update_snapshot(ConversationSnapshot(mode="chat", goal="stale"))
        self.assertNotIn("chat-1", registry.contexts)
        self.assertEqual(registry.for_session("chat-1").snapshot.goal, "old")

    def test_session_load_runs_outside_registry_lock(self) -> None:
        seen_locked: list[bool] = []
        registry: ConversationRegistry

        def load(_session_id: str) -> None:
            seen_locked.append(registry.lock.locked())
            return None

        registry = _registry({}, load=load)
        registry.for_session("chat-1")
        self.assertEqual(seen_locked, [False])

    def test_forget_during_load_does_not_leave_cached_context(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        results: list[object] = []
        registry: ConversationRegistry

        def load(_session_id: str) -> None:
            entered.set()
            release.wait(timeout=5)
            return None

        registry = _registry({}, load=load)
        worker = threading.Thread(target=lambda: results.append(registry.for_session("s1")), daemon=True)
        worker.start()
        self.assertTrue(entered.wait(timeout=5))
        registry.forget("s1")
        release.set()
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(results), 1)
        self.assertNotIn("s1", registry.contexts)


if __name__ == "__main__":
    unittest.main()
