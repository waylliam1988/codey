from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codey.storage.ui_state_store import UiStateConflict, UiStateStore


class UiStateStoreTests(unittest.TestCase):


    def test_research_metadata_survives_round_trip_without_chat_content(self) -> None:
        # Research links remain UI metadata; transcripts come from durable history.
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

            store.save(state, base_revision=0)
            loaded = store.load()
            session = loaded["sessions"][0]

            self.assertEqual(len(session["researchRuns"]), 2)
            self.assertEqual(session["researchRuns"][0]["runId"], "run-1")
            self.assertNotIn("unknown", session["researchRuns"][0])
            self.assertFalse(session["researchRuns"][0]["restoreable"])
            self.assertEqual(session["researchRuns"][0]["coverage"], {"opened": 1})
            self.assertEqual(session["research"], True)
            self.assertNotIn("messages", session)

    def test_research_runs_are_capped_to_frontend_restore_window(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = UiStateStore(td)
            store.save({
                "active_id": "chat-research",
                "updated_at": 10,
                "revision": 0,
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
            }, base_revision=0)

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
                "revision": 0,
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

            saved = store.save(state, base_revision=0)

            loaded = store.load()
            self.assertEqual(loaded["active_id"], state["active_id"])
            self.assertEqual(loaded["sessions"], saved["sessions"])
            self.assertEqual(loaded["projects"], saved["projects"])
            self.assertEqual(loaded["revision"], 1)
            self.assertEqual(saved["revision"], 1)
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
                "revision": 0,
            }, base_revision=0)

            self.assertEqual(store.load(), {
                "active_id": "42",
                "sessions": [],
                "projects": [],
                "updated_at": 17,
                "revision": 1,
            })

    def test_save_compares_against_cached_state_not_disk(self) -> None:
        # Regression: save() used to re-read and re-parse the file on every
        # call, doubling the IO of the hottest UI-state path.
        with tempfile.TemporaryDirectory() as td:
            store = UiStateStore(td)
            state = {
                "active_id": "chat-1",
                "updated_at": 5,
                "revision": 0,
                "sessions": [],
                "projects": [],
            }

            with mock.patch.object(
                store,
                "load",
                wraps=store.load,
            ) as load_spy:
                saved = store.save(state, base_revision=0)
                self.assertEqual(saved["revision"], 1)
                # Identical content with matching base succeeds idempotently.
                same = store.save(state, base_revision=1)
                self.assertEqual(same["revision"], 1)
                bumped = dict(state, active_id="chat-2")
                saved2 = store.save(bumped, base_revision=1)
                self.assertEqual(saved2["revision"], 2)

                # Exactly one seed read (first save with a cold cache); the
                # remaining comparisons run against memory.
                self.assertEqual(load_spy.call_count, 1)
                self.assertEqual(store.load()["revision"], 2)
                # After the explicit reload, the cache reflects disk again.
                store.save(bumped, base_revision=2)
                self.assertEqual(load_spy.call_count, 2)

    def test_failed_write_keeps_previous_cache_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = UiStateStore(td)
            first = {
                "active_id": "chat-1",
                "updated_at": 1,
                "revision": 0,
                "sessions": [],
                "projects": [],
            }
            saved = store.save(first, base_revision=0)

            second = dict(first, active_id="chat-2")
            with mock.patch(
                "codey.storage.ui_state_store.write_json_atomic",
                side_effect=OSError("disk full"),
            ), self.assertRaises(OSError):
                store.save(second, base_revision=saved["revision"])

            self.assertEqual(store._cached_state, saved)

    def test_same_base_conflict_with_different_content_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = UiStateStore(td)
            first = {
                "active_id": "new",
                "updated_at": 200,
                "revision": 0,
                "sessions": [],
                "projects": [],
            }
            saved = store.save(first, base_revision=0)
            self.assertEqual(saved["revision"], 1)
            other = {
                "active_id": "old",
                "updated_at": 200,
                "revision": 0,
                "sessions": [{"id": "old", "title": "older"}],
                "projects": [],
            }
            with self.assertRaises(UiStateConflict) as ctx:
                store.save(other, base_revision=0)
            self.assertEqual(ctx.exception.current_revision, 1)
            self.assertEqual(store.load()["active_id"], "new")

    def test_stale_base_conflict_reports_current_revision(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = UiStateStore(td)
            first = {
                "active_id": "first",
                "updated_at": 200,
                "revision": 0,
                "sessions": [],
                "projects": [],
            }
            saved1 = store.save(first, base_revision=0)
            second = {
                "active_id": "second",
                "updated_at": 200,
                "revision": 0,
                "sessions": [],
                "projects": [],
            }
            saved2 = store.save(second, base_revision=saved1["revision"])
            self.assertEqual(saved2["revision"], 2)
            stale = dict(first, active_id="stale")
            with self.assertRaises(UiStateConflict) as ctx:
                store.save(stale, base_revision=1)
            self.assertEqual(ctx.exception.current_revision, 2)
            self.assertEqual(store.load()["active_id"], "second")

    def test_identical_content_with_stale_base_succeeds_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = UiStateStore(td)
            first = {
                "active_id": "first",
                "updated_at": 200,
                "revision": 0,
                "sessions": [],
                "projects": [],
            }
            saved = store.save(first, base_revision=0)
            # Same content, stale base: idempotent success, no new revision.
            same = store.save(first, base_revision=0)
            self.assertEqual(same["revision"], saved["revision"])
            self.assertEqual(store.load()["revision"], saved["revision"])

    def test_state_schema_is_whitelisted_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = UiStateStore(td)
            store.save({
                "active_id": "chat-1",
                "updated_at": 300,
                "revision": 0,
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
            }, base_revision=0)

            state = store.load()

            self.assertNotIn("ignored", state)
            self.assertNotIn("extra", state["sessions"][0])
            self.assertNotIn("messages", state["sessions"][0])
            self.assertEqual(len(state["sessions"][0]["terminalRuns"]), 32)
            self.assertEqual(state["projects"][0], {
                "id": "project-1",
                "name": "demo",
                "path": "E:/demo",
                "expanded": False,
                "createdAt": 2,
            })


    def test_deleted_identities_cannot_be_recreated_after_lost_acknowledgement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = UiStateStore(directory)
            store.save({'active_id':'lost','sessions':[{'id':'lost'}],
                        'projects':[{'id':'project','path':'E:/project'}]}, base_revision=0)
            # Creation succeeded, but its client never received that response.
            deleted = store.forget_session('lost')
            current = store.save({**deleted, 'projects':[]}, base_revision=deleted['revision'])
            reopened = UiStateStore(directory)
            self.assertEqual(reopened.load()['deleted_ids'], {'sessions':['lost'],'projects':['project']})
            for field, row in [('sessions', {'id':'lost'}), ('projects', {'id':'project','path':'E:/project'})]:
                with self.assertRaises(UiStateConflict):
                    reopened.save({**current, field:[row], 'deleted_ids':{}}, base_revision=current['revision'])
            self.assertEqual(reopened.load()['revision'], current['revision'])


if __name__ == "__main__":
    unittest.main()
