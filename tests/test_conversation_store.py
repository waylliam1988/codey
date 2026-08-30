from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from codey.app import server
from codey.storage.conversation_store import (
    MAX_PERSISTED_CONVERSATIONS,
    ConversationStore,
)
from codey.agents.handoff import ConversationSnapshot


class ConversationStoreTests(unittest.TestCase):
    def test_round_trip_keeps_bounded_facts_not_full_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = ConversationStore(td)
            context = store.load("chat-1")
            context.begin_window("deepseek", "chat")
            context.record_exchange(
                "u" * 10_000,
                "r" * 10_000,
                ConversationSnapshot(
                    mode="chat",
                    goal="Keep ORANGE-417",
                    provider_id="deepseek",
                    latest_user="u" * 10_000,
                    latest_reply="r" * 10_000,
                ),
            )
            store.save("chat-1", context)

            restored = store.load("chat-1")
            raw = store.path_for("chat-1").read_text(encoding="utf-8")

        self.assertTrue(restored.initialized)
        self.assertEqual(restored.snapshot.goal, "Keep ORANGE-417")
        self.assertLessEqual(len(restored.snapshot.latest_user), 2_020)
        self.assertLessEqual(len(restored.snapshot.latest_reply), 2_020)
        self.assertNotIn("provider-controls", raw)
        self.assertLess(len(raw), 20_000)

    def test_state_reloads_same_session_and_keeps_new_session_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            first = server.AppContext(td)
            context = first.conversation_for("chat-1")
            context.begin_window("stepfun", "project", "E:/demo")
            context.update_snapshot(ConversationSnapshot(
                mode="project",
                goal="Keep ORANGE-417",
                project="E:/demo",
                provider_id="stepfun",
                changed_files=("app.py",),
                checks_passed=True,
            ))

            second = server.AppContext(td)
            restored = second.conversation_for("chat-1")
            unrelated = second.conversation_for("chat-2")

        self.assertEqual(restored.snapshot.goal, "Keep ORANGE-417")
        self.assertEqual(restored.snapshot.changed_files, ("app.py",))
        self.assertTrue(restored.snapshot.checks_passed)
        self.assertEqual(unrelated.snapshot.goal, "")

    def test_forget_deletes_memory_and_disk_state(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = server.AppContext(td)
            state.conversation_for("chat-1").update_snapshot(
                ConversationSnapshot(mode="chat", goal="temporary")
            )
            path = state.conversation_registry.store.path_for("chat-1")
            self.assertTrue(path.is_file())

            state.forget_conversation("chat-1")

            self.assertFalse(path.exists())
            self.assertEqual(state.conversation_for("chat-1").snapshot.goal, "")

    def test_forgotten_context_cannot_recreate_deleted_state(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = server.AppContext(td)
            old_context = state.conversation_for("chat-1")
            old_context.update_snapshot(
                ConversationSnapshot(mode="chat", goal="old")
            )
            path = state.conversation_registry.store.path_for("chat-1")

            state.forget_conversation("chat-1")
            old_context.update_snapshot(
                ConversationSnapshot(mode="chat", goal="must not return")
            )

            self.assertFalse(path.exists())
            fresh = state.conversation_for("chat-1")
            fresh.update_snapshot(ConversationSnapshot(mode="chat", goal="new"))
            self.assertEqual(state.conversation_registry.store.load("chat-1").snapshot.goal, "new")

    def test_forget_wins_over_save_already_in_progress(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = server.AppContext(td)
            context = state.conversation_for("chat-1")
            context.update_snapshot(ConversationSnapshot(mode="chat", goal="old"))
            path = state.conversation_registry.store.path_for("chat-1")
            save_started = threading.Event()
            allow_save = threading.Event()
            real_save = state.conversation_registry.store.save

            def blocking_save(session_id, value):
                save_started.set()
                self.assertTrue(allow_save.wait(2))
                real_save(session_id, value)

            with mock.patch.object(
                state.conversation_registry.store,
                "save",
                side_effect=blocking_save,
            ):
                writer = threading.Thread(
                    target=lambda: context.update_snapshot(
                        ConversationSnapshot(mode="chat", goal="late")
                    )
                )
                writer.start()
                self.assertTrue(save_started.wait(2))
                forgetter = threading.Thread(
                    target=state.forget_conversation,
                    args=("chat-1",),
                )
                forgetter.start()
                allow_save.set()
                writer.join(2)
                forgetter.join(2)

            self.assertFalse(writer.is_alive())
            self.assertFalse(forgetter.is_alive())
            self.assertFalse(path.exists())

    def test_corrupt_unknown_and_other_session_state_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = ConversationStore(td)
            store.path_for("broken").parent.mkdir(parents=True)
            store.path_for("broken").write_text("not json", encoding="utf-8")
            store.path_for("future").write_text(
                json.dumps({"schema_version": 999, "snapshot": {"goal": "no"}}),
                encoding="utf-8",
            )

            self.assertEqual(store.load("broken").snapshot.goal, "")
            self.assertEqual(store.load("future").snapshot.goal, "")
            self.assertEqual(store.load("missing").snapshot.goal, "")

    def test_store_prunes_only_older_conversations(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = ConversationStore(td)
            for index in range(MAX_PERSISTED_CONVERSATIONS + 2):
                context = store.load(f"chat-{index}")
                context.update_snapshot(
                    ConversationSnapshot(mode="chat", goal=f"goal-{index}")
                )
                store.save(f"chat-{index}", context)

            paths = list((Path(td) / "conversations").glob("*.json"))

        self.assertEqual(len(paths), MAX_PERSISTED_CONVERSATIONS)

    def test_restart_opens_fresh_provider_chat_with_silent_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            first = server.AppContext(td)
            context = first.conversation_for("chat-1")
            context.begin_window("deepseek", "chat")
            context.record_exchange(
                "Remember the code",
                "I will remember it",
                ConversationSnapshot(
                    mode="chat",
                    goal="Keep ORANGE-417",
                    provider_id="deepseek",
                    latest_user="Remember the code",
                    latest_reply="I will remember it",
                ),
            )
            first.save_ui_state({
                "active_id": "chat-1",
                "updated_at": 1,
                "revision": 1,
                "sessions": [{
                    "id": "chat-1",
                    "title": "Memory test",
                    "messages": [
                        {"type": "user", "text": "Earlier UI-only detail"},
                        {"type": "asst", "text": "Keep EARLY-UI-MARKER"},
                        {"type": "user", "text": "Continue"},
                    ],
                    "terminalRuns": [],
                    "createdAt": 0,
                    "projectId": None,
                    "provider": "deepseek",
                }],
                "projects": [],
            })

            restarted = server.AppContext(td)
            events = restarted.subscribe()
            provider = mock.Mock()
            provider.name = "DeepSeek Web"
            provider.send.return_value = "ORANGE-417"
            with (
                mock.patch.object(server, "STATE", restarted),
                mock.patch.object(restarted, "get_provider", return_value=provider),
                mock.patch.object(server, "_run_consensus", return_value=None),
            ):
                server._run_task("chat-1", None, "Continue", 4, False, "deepseek")

            prompt = provider.send.call_args.args[0]
            emitted = []
            while not events.empty():
                emitted.append(events.get_nowait())

        provider.new_chat.assert_called_once_with()
        self.assertIn("Factual handoff", prompt)
        self.assertIn("ORANGE-417", prompt)
        self.assertIn("recent_visible_conversation", prompt)
        self.assertIn("EARLY-UI-MARKER", prompt)
        self.assertNotIn("User: Continue", prompt)
        self.assertFalse(any(event.get("type") == "info" for event in emitted))

    def test_persistence_failure_does_not_break_live_conversation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = server.AppContext(td)
            context = state.conversation_for("chat-1")

            with mock.patch.object(
                state.conversation_registry.store,
                "save",
                side_effect=OSError("disk full"),
            ):
                context.update_snapshot(
                    ConversationSnapshot(mode="chat", goal="keep working")
                )

            self.assertEqual(context.snapshot.goal, "keep working")


if __name__ == "__main__":
    unittest.main()