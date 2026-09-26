"""Red-first: learning switch must fail closed on corrupt settings.

Bug 1: GhostInbox._read_settings_unlocked returns learning_enabled=True
for both missing and corrupt configs; settlement defaults True on read
error; ghost_experiences continues retrieval. This lets a corrupted file
re-enable learning after the user turned it off.
"""
from __future__ import annotations

import contextlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from codey.ghost.inbox import GhostInboxStore
from codey.ghost.observations import GhostObservationStore
from codey.operations import ghost_context
from codey.operations.task_phases import settlement


def _inbox(td: str) -> GhostInboxStore:
    return GhostInboxStore(Path(td))


class LearningSwitchFailClosedTests(unittest.TestCase):
    def test_missing_file_defaults_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertTrue(_inbox(td).learning_enabled())

    def test_corrupt_settings_does_not_reenable(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = _inbox(td)
            self.assertTrue(store.set_learning_enabled(False))
            self.assertFalse(store.learning_enabled())
            store.settings_path.write_text("{broken", encoding="utf-8")
            # Fail-closed: must NOT silently return True.
            try:
                value = store.learning_enabled()
            except Exception:
                return
            self.assertFalse(
                value,
                "corrupt settings must not read as learning_enabled=True",
            )

    def test_corrupt_settings_keeps_file_for_repair(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = _inbox(td)
            store.set_learning_enabled(False)
            store.settings_path.write_text("{broken", encoding="utf-8")
            with contextlib.suppress(Exception):
                store.learning_enabled()
            # The corrupt file must stay in place so the user can inspect
            # it; silently quarantining + returning enabled hides the error.
            self.assertTrue(
                store.settings_path.exists(),
                "corrupt settings file must not vanish silently",
            )

    def test_string_false_is_not_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = _inbox(td)
            store.directory.mkdir(parents=True, exist_ok=True)
            store.settings_path.write_text(
                '{"schema_version": 1, "learning_enabled": "false"}',
                encoding="utf-8",
            )
            try:
                value = store.learning_enabled()
            except Exception:
                return
            self.assertFalse(
                value,
                'string "false" must not coerce to True via bool(...)',
            )

    def test_settlement_skips_write_on_settings_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = GhostObservationStore(Path(td))
            inbox = mock.Mock()
            inbox.learning_enabled.side_effect = ValueError("settings corrupt")
            state = SimpleNamespace(
                ghost_inbox=inbox,
                ghost_observations=store,
            )
            appended = []
            with mock.patch.object(
                store, "append_completed",
                side_effect=lambda **kw: appended.append(kw) or True,
            ):
                ok, status = settlement._persist_observation_row(
                    state, run_id="r1", session_id="s", project="",
                    mode="chat", user_text="hi", assistant_text="hello",
                    stop_reason="done", provider_id="local",
                )
            self.assertEqual(appended, [])
            self.assertNotEqual(status, "committed")

    def test_retrieval_stops_on_settings_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = GhostObservationStore(Path(td))
            store.append_completed(
                run_id="r1", session_id="s", mode="chat",
                user_text="hi", assistant_text="hello",
                stop_reason="done", provider_id="local",
            )
            inbox = mock.Mock()
            inbox.learning_enabled.side_effect = ValueError("settings corrupt")
            state = SimpleNamespace(ghost_inbox=inbox, ghost_observations=store)
            with mock.patch.object(
                store, "read_committed",
                wraps=store.read_committed,
            ) as spy:
                text = ghost_context.ghost_experiences(
                    state, session_id="s", project="", query="hi",
                )
            self.assertEqual(text, "")
            # Must not return old observations when the switch cannot be read.
            self.assertEqual(spy.call_count, 0)

    def test_repair_recovers_after_corruption(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = _inbox(td)
            store.set_learning_enabled(False)
            store.settings_path.write_text("{broken", encoding="utf-8")
            with contextlib.suppress(Exception):
                store.learning_enabled()
            self.assertTrue(store.set_learning_enabled(True))
            self.assertTrue(store.learning_enabled())

    def test_off_corrupt_blocks_write_retrieval_and_background(self) -> None:
        """贯穿：关闭学习 → 配置损坏 → 下一轮三不写/不检/不学；修复后恢复."""
        from codey.ghost.learning_loop import GhostLearningLoop
        from codey.operations import ghost_post_turn

        with tempfile.TemporaryDirectory() as td:
            inbox = GhostInboxStore(Path(td))
            observations = GhostObservationStore(Path(td))
            self.assertTrue(inbox.set_learning_enabled(False))
            self.assertFalse(inbox.learning_enabled())
            # Seed one committed row while the switch is cleanly off so
            # retrieval has something it must NOT return once corrupt.
            observations.append_completed(
                run_id="seed", session_id="s", mode="chat",
                user_text="hi", assistant_text="hello",
                stop_reason="done", provider_id="local",
            )
            inbox.settings_path.write_text("{broken", encoding="utf-8")

            state = SimpleNamespace(
                ghost_inbox=inbox,
                ghost_observations=observations,
                emit=lambda _e: None,
            )
            # 1) settlement must not write.
            with mock.patch.object(
                observations, "append_completed",
                wraps=observations.append_completed,
            ) as spy_append:
                ok, status = settlement._persist_observation_row(
                    state, run_id="r2", session_id="s", project="",
                    mode="chat", user_text="hi2", assistant_text="hello2",
                    stop_reason="done", provider_id="local",
                )
            self.assertEqual(spy_append.call_count, 0)
            self.assertEqual(status, "settings_error")
            self.assertFalse(ok)
            # 2) retrieval must not serve the seeded row.
            with mock.patch.object(
                observations, "read_committed",
                wraps=observations.read_committed,
            ) as spy_read:
                text = ghost_context.ghost_experiences(
                    state, session_id="s", project="", query="hi",
                )
            self.assertEqual(text, "")
            self.assertEqual(spy_read.call_count, 0)
            # 3) background learning must stay off (both gates).
            self.assertFalse(
                ghost_post_turn._ghost_learning_enabled(state)
            )
            loop2 = GhostLearningLoop(
                signal_store=mock.Mock(), inbox_store=inbox,
                hebbian_store=mock.Mock(),
            )
            result = loop2.learn_from_turn(
                SimpleNamespace(
                    user_text="hi", assistant_text="hello",
                    session_id="s", run_id="r2", project="",
                    provider_id="local",
                ),
                provider_factory=None,
            )
            self.assertTrue(result.ok)
            self.assertIn("settings_corrupt", result.skipped_reason)
            # 4) explicit repair recovers.
            self.assertTrue(inbox.set_learning_enabled(True))
            self.assertTrue(inbox.learning_enabled())


if __name__ == "__main__":
    unittest.main()
