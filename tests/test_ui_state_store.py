from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codey.ui_state_store import UiStateStore


class UiStateStoreTests(unittest.TestCase):
    def test_round_trip_visible_ui_state(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = UiStateStore(td)
            state = {
                "active_id": "chat-1",
                "updated_at": 123,
                "revision": 7,
                "sessions": [{
                    "id": "chat-1",
                    "title": "你好",
                    "messages": [{"type": "user", "text": "你好"}],
                    "terminalRuns": ["run-1"],
                    "createdAt": 100,
                    "projectId": "project-1",
                    "provider": "deepseek",
                }],
                "projects": [{
                    "id": "project-1",
                    "name": "demo",
                    "path": "E:/demo",
                    "expanded": True,
                    "createdAt": 101,
                }],
            }

            store.save(state)

            self.assertEqual(store.load(), state)
            self.assertEqual(Path(td, "ui-state.json").is_file(), True)

    def test_missing_or_wrong_schema_returns_empty_state(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = UiStateStore(td)
            self.assertEqual(store.load(), {
                "active_id": "",
                "sessions": [],
                "projects": [],
                "updated_at": 0,
                "revision": 0,
            })

            Path(td, "ui-state.json").write_text('{"schema_version":999}', encoding="utf-8")

            self.assertEqual(store.load(), {
                "active_id": "",
                "sessions": [],
                "projects": [],
                "updated_at": 0,
                "revision": 0,
            })

    def test_non_list_collections_are_sanitized(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = UiStateStore(td)

            store.save({
                "active_id": 42,
                "sessions": {"bad": True},
                "projects": None,
                "updated_at": "17.9",
                "revision": "3",
            })

            self.assertEqual(store.load(), {
                "active_id": "42",
                "sessions": [],
                "projects": [],
                "updated_at": 17,
                "revision": 3,
            })

    def test_older_save_cannot_overwrite_newer_state(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = UiStateStore(td)
            newer = {
                "active_id": "new",
                "updated_at": 200,
                "revision": 1,
                "sessions": [{"id": "new", "title": "newer", "messages": [], "terminalRuns": [], "createdAt": 0, "projectId": None, "provider": ""}],
                "projects": [],
            }
            older = {
                "active_id": "old",
                "updated_at": 100,
                "revision": 99,
                "sessions": [{"id": "old", "title": "older"}],
                "projects": [],
            }

            store.save(newer)
            store.save(older)

            self.assertEqual(store.load(), newer)

    def test_lower_revision_cannot_overwrite_same_millisecond_state(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = UiStateStore(td)
            newer = {
                "active_id": "new",
                "updated_at": 200,
                "revision": 2,
                "sessions": [{"id": "new", "title": "newer", "messages": [], "terminalRuns": [], "createdAt": 0, "projectId": None, "provider": ""}],
                "projects": [],
            }
            older = {
                "active_id": "old",
                "updated_at": 200,
                "revision": 1,
                "sessions": [{"id": "old", "title": "older"}],
                "projects": [],
            }

            store.save(newer)
            store.save(older)

            self.assertEqual(store.load(), newer)

    def test_higher_revision_can_update_same_millisecond_state(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = UiStateStore(td)
            first = {
                "active_id": "first",
                "updated_at": 200,
                "revision": 1,
                "sessions": [{"id": "first", "title": "first"}],
                "projects": [],
            }
            second = {
                "active_id": "second",
                "updated_at": 200,
                "revision": 2,
                "sessions": [{"id": "second", "title": "second", "messages": [], "terminalRuns": [], "createdAt": 0, "projectId": None, "provider": ""}],
                "projects": [],
            }

            store.save(first)
            store.save(second)

            self.assertEqual(store.load(), second)

    def test_state_schema_is_whitelisted_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = UiStateStore(td)
            store.save({
                "active_id": "chat-1",
                "updated_at": 300,
                "revision": 1,
                "ignored": "no",
                "sessions": [{
                    "id": "chat-1",
                    "title": "hello",
                    "extra": "drop",
                    "messages": [
                        {"type": "user", "text": "hi", "secret": "drop"},
                        "bad",
                        {"type": "changes", "files": [{"path": "a.py", "status": "M", "extra": "drop"}]},
                    ],
                    "terminalRuns": [str(i) for i in range(40)],
                    "createdAt": 1,
                    "projectId": None,
                    "provider": "deepseek",
                }],
                "projects": [{
                    "id": "project-1",
                    "name": "demo",
                    "path": "E:/demo",
                    "expanded": False,
                    "createdAt": 2,
                    "extra": "drop",
                }],
            })

            state = store.load()

            self.assertNotIn("ignored", state)
            self.assertNotIn("extra", state["sessions"][0])
            self.assertNotIn("secret", state["sessions"][0]["messages"][0])
            self.assertEqual(len(state["sessions"][0]["terminalRuns"]), 32)
            self.assertEqual(state["sessions"][0]["messages"][1]["files"], [{
                "path": "a.py",
                "status": "M",
                "additions": 0,
                "deletions": 0,
            }])
            self.assertEqual(state["projects"][0], {
                "id": "project-1",
                "name": "demo",
                "path": "E:/demo",
                "expanded": False,
                "createdAt": 2,
            })


if __name__ == "__main__":
    unittest.main()
