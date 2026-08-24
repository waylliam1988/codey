from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codey.ui_state_store import UiStateStore


class UiStateStoreTests(unittest.TestCase):
    def test_research_history_and_pending_tool_fields_survive_round_trip(self) -> None:
        # Regression: the session/message sanitizers used to strip research
        # runs and pending-tool fields, and the UI's restore-then-overwrite
        # flow then erased all research history on every restart.
        with tempfile.TemporaryDirectory() as td:
            store = UiStateStore(td)
            state = {
                "active_id": "chat-research",
                "updated_at": 10,
                "revision": 1,
                "sessions": [{
                    "id": "chat-research",
                    "title": "Research chat",
                    "messages": [
                        {
                            "type": "tool",
                            "text": "approve shell?",
                            "toolKey": "shell-key-1",
                            "activity": "awaiting approval",
                            "pending": True,
                        },
                        {"type": "user", "text": "run it"},
                    ],
                    "terminalRuns": ["run-9"],
                    "createdAt": 5,
                    "provider": "deepseek",
                    "researchRuns": [
                        {
                            "runId": "run-1",
                            "synthesisId": "synth-1",
                            "notesCreated": ["note-1"],
                            "sourceUrls": ["https://example.com/helium"],
                            "sourcesRead": 1,
                            "queries": ["helium"],
                            "searchResults": [{"title": "Helium", "extra": "kept-bounded"}],
                            "coverage": {"opened": 1},
                            "receipt": "done",
                            "restoreable": True,
                            "createdAt": 7,
                            "unknown": "drop",
                        },
                        {
                            "runId": "run-2",
                            "synthesisId": "synth-2",
                            "qualityWarnings": ["stale"],
                            "createdAt": 8,
                        },
                    ],
                    "research": {"topic": "helium", "lastRunId": "run-1"},
                }],
                "projects": [],
            }

            store.save(state)
            loaded = store.load()
            session = loaded["sessions"][0]

            self.assertEqual(len(session["researchRuns"]), 2)
            self.assertEqual(session["researchRuns"][0]["runId"], "run-1")
            self.assertNotIn("unknown", session["researchRuns"][0])
            self.assertFalse(session["researchRuns"][0]["restoreable"])
            self.assertEqual(session["researchRuns"][0]["coverage"], {"opened": 1})
            self.assertEqual(session["research"], True)
            message = session["messages"][0]
            for key in ("toolKey", "activity", "pending"):
                self.assertIn(key, message)
            self.assertTrue(message["pending"])

    def test_research_runs_are_capped_to_frontend_restore_window(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = UiStateStore(td)
            store.save({
                "active_id": "chat-research",
                "updated_at": 10,
                "revision": 1,
                "sessions": [{
                    "id": "chat-research",
                    "title": "Research chat",
                    "messages": [],
                    "terminalRuns": [],
                    "createdAt": 5,
                    "provider": "deepseek",
                    "researchRuns": [
                        {"runId": f"run-{index}", "createdAt": index}
                        for index in range(40)
                    ],
                }],
                "projects": [],
            })

            runs = store.load()["sessions"][0]["researchRuns"]

        self.assertEqual(len(runs), 32)
        self.assertEqual(runs[0]["runId"], "run-8")
        self.assertEqual(runs[-1]["runId"], "run-39")

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
                    "messages": [
                        {"type": "turn", "n": 17, "note": "(done)"},
                        {"type": "user", "text": "你好"},
                    ],
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

    def test_shell_request_risk_fields_are_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = UiStateStore(td)
            store.save({
                "active_id": "chat-1",
                "updated_at": 1,
                "revision": 1,
                "sessions": [{
                    "id": "chat-1",
                    "title": "New chat",
                    "messages": [{
                        "type": "shell_request",
                        "id": "shell-1",
                        "command": "npm install",
                        "cwd": ".",
                        "riskLabel": "dependency_install",
                        "riskTitle": "Dependency install",
                        "riskDetail": "May download packages.",
                    }],
                    "terminalRuns": [],
                    "createdAt": 0,
                    "projectId": None,
                    "provider": "deepseek",
                }],
                "projects": [],
            })

            message = store.load()["sessions"][0]["messages"][0]

        self.assertEqual(message["riskLabel"], "dependency_install")
        self.assertEqual(message["riskTitle"], "Dependency install")
        self.assertEqual(message["riskDetail"], "May download packages.")

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

    def test_visible_session_excerpt_uses_recent_visible_chat_only(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = UiStateStore(td)
            store.save({
                "active_id": "chat-1",
                "updated_at": 1,
                "revision": 1,
                "sessions": [{
                    "id": "chat-1",
                    "title": "Breathing app",
                    "messages": [
                        {"type": "turn", "n": 1},
                        {"type": "tool", "kind": "read_file", "result": "secret tool output"},
                        {"type": "shell_result", "output": "long shell output"},
                        {"type": "user", "text": "Yesterday question"},
                        {"type": "asst", "text": "Yesterday answer"},
                        {"type": "review", "text": "Review approved"},
                        {"type": "done", "text": "DONE · checks passed"},
                        {"type": "changes", "count": 2, "files": [{"path": "app.py"}, {"path": "test_app.py"}]},
                        {"type": "user", "text": "Continue today"},
                    ],
                    "terminalRuns": [],
                    "createdAt": 0,
                    "projectId": None,
                    "provider": "deepseek",
                }],
                "projects": [],
            })

            excerpt = store.visible_session_excerpt("chat-1", current_request="Continue today")

            self.assertIn("Title: Breathing app", excerpt)
            self.assertIn("User: Yesterday question", excerpt)
            self.assertIn("Assistant: Yesterday answer", excerpt)
            self.assertIn("Review: Review approved", excerpt)
            self.assertIn("Done: DONE · checks passed", excerpt)
            self.assertIn("Changes: 2 files: app.py, test_app.py", excerpt)
            self.assertNotIn("Continue today", excerpt)
            self.assertNotIn("secret tool output", excerpt)
            self.assertNotIn("long shell output", excerpt)

    def test_visible_session_excerpt_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = UiStateStore(td)
            store.save({
                "active_id": "chat-1",
                "updated_at": 1,
                "revision": 1,
                "sessions": [{
                    "id": "chat-1",
                    "title": "New chat",
                    "messages": [
                        {"type": "user", "text": "x" * 10_000},
                        {"type": "asst", "text": "y" * 10_000},
                    ],
                    "terminalRuns": [],
                    "createdAt": 0,
                    "projectId": None,
                    "provider": "deepseek",
                }],
                "projects": [],
            })

            excerpt = store.visible_session_excerpt("chat-1", limit=2_000)

            self.assertLessEqual(len(excerpt), 2_000)

    def test_visible_session_excerpt_counts_visible_messages_after_filtering(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = UiStateStore(td)
            store.save({
                "active_id": "chat-1",
                "updated_at": 1,
                "revision": 1,
                "sessions": [{
                    "id": "chat-1",
                    "title": "Project review",
                    "messages": [
                        {"type": "user", "text": "Find project bugs"},
                        {"type": "asst", "text": "Keep VISIBLE-BEFORE-NOISE"},
                        *[
                            {"type": "tool", "kind": "read_file", "result": f"noise {index}"}
                            for index in range(30)
                        ],
                        {"type": "user", "text": "Continue"},
                    ],
                    "terminalRuns": [],
                    "createdAt": 0,
                    "projectId": None,
                    "provider": "deepseek",
                }],
                "projects": [],
            })

            excerpt = store.visible_session_excerpt("chat-1", current_request="Continue")

            self.assertIn("User: Find project bugs", excerpt)
            self.assertIn("Assistant: Keep VISIBLE-BEFORE-NOISE", excerpt)
            self.assertNotIn("noise 29", excerpt)
            self.assertNotIn("User: Continue", excerpt)


if __name__ == "__main__":
    unittest.main()
