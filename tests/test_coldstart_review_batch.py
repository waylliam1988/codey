from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codey.app import http_plumbing
from codey.app.context import MAX_CHANGE_TRACKERS, AppContext
from codey.storage.ui_state_store import UiStateStore
from codey.workspace.paths import bounded_directory_entries, safe_join


class ColdstartReviewBatchTests(unittest.TestCase):
    def test_safe_join_rejects_windows_reserved_names_on_nt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            import codey.workspace.paths as paths

            with mock.patch.object(paths.os, "name", "nt"):
                with self.assertRaisesRegex(ValueError, "reserved device name"):
                    safe_join(root, "NUL")
                with self.assertRaisesRegex(ValueError, "reserved device name"):
                    safe_join(root, "subdir/CON.txt")
                with self.assertRaisesRegex(ValueError, "reserved device name"):
                    safe_join(root, "COM1")
                # Trailing dots/spaces are still the same device on Windows.
                with self.assertRaisesRegex(ValueError, "reserved device name"):
                    safe_join(root, "NUL.txt")
            # POSIX keeps the old behavior: the gate itself is a no-op.
            with mock.patch.object(paths.os, "name", "posix"):
                paths._reject_windows_reserved_names("NUL", label="project root")
                paths._reject_windows_reserved_names("subdir/CON.txt", label="project root")

    def test_bounded_entries_filter_before_budget(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for name in (".hidden-a", ".hidden-b", ".hidden-c"):
                (root / name).write_text("x\n", encoding="utf-8")
            (root / "app.py").write_text("print('ok')\n", encoding="utf-8")
            (root / "main.py").write_text("print('ok')\n", encoding="utf-8")

            entries, truncated = bounded_directory_entries(
                root, 2, include_hidden=False
            )
            names = sorted(entry.name for entry in entries)
            self.assertEqual(names, ["app.py", "main.py"])
            self.assertFalse(truncated)

    def test_visible_session_excerpt_uses_cache_not_disk(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = UiStateStore(td)
            store.save({
                "active_id": "chat-1",
                "updated_at": 1,
                "revision": 1,
                "sessions": [{
                    "id": "chat-1",
                    "title": "Cached title",
                    "messages": [{"type": "user", "text": "hello"}],
                    "terminalRuns": [],
                    "createdAt": 0,
                    "projectId": None,
                    "provider": "deepseek",
                }],
                "projects": [],
            })
            from codey.storage import local_store  # noqa: F401 (documents the wrong patch target)

            import codey.storage.ui_state_store as ui_mod

            with mock.patch.object(
                ui_mod, "read_json_strict", side_effect=OSError("disk gone")
            ):
                excerpt = store.visible_session_excerpt("chat-1")
            self.assertIn("Cached title", excerpt)
            self.assertIn("User: hello", excerpt)

    def test_change_trackers_evict_oldest_only(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = AppContext(td)
            try:
                first = Path(td) / "proj-00"
                first.mkdir()
                state.change_tracker_for(first, persistent=False)
                for index in range(1, MAX_CHANGE_TRACKERS + 1):
                    proj = Path(td) / f"proj-{index:02d}"
                    proj.mkdir()
                    state.change_tracker_for(proj, persistent=False)
                self.assertLessEqual(len(state.change_trackers), MAX_CHANGE_TRACKERS)
                self.assertNotIn(str(first.resolve()), state.change_trackers)
                # Recently touched tracker survives: touch proj-01 then add one more.
                proj01 = Path(td) / "proj-01"
                state.change_tracker_for(proj01, persistent=False)
                extra = Path(td) / "proj-extra"
                extra.mkdir()
                state.change_tracker_for(extra, persistent=False)
                self.assertIn(str(proj01.resolve()), state.change_trackers)
            finally:
                state.close()

    def test_static_cache_evicts_oldest_entries(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            paths = []
            for index in range(http_plumbing.MAX_STATIC_CACHE_ENTRIES + 5):
                path = Path(td) / f"asset-{index:03d}.js"
                path.write_text(f"console.log({index});", encoding="utf-8")
                paths.append(path)
            with mock.patch.object(
                http_plumbing, "MAX_STATIC_CACHE_ENTRIES", 4
            ):
                with http_plumbing._STATIC_CACHE_LOCK:
                    http_plumbing._STATIC_CACHE.clear()
                    import codey.app.http_plumbing as plumbing_mod

                    plumbing_mod._STATIC_CACHE_BYTES = 0
                for path in paths:
                    http_plumbing._cached_file(path, transform_name="raw")
                with http_plumbing._STATIC_CACHE_LOCK:
                    self.assertLessEqual(
                        len(http_plumbing._STATIC_CACHE),
                        http_plumbing.MAX_STATIC_CACHE_ENTRIES,
                    )
                    first_key = (str(paths[0]), "raw")
                    self.assertNotIn(first_key, http_plumbing._STATIC_CACHE)
                    last_key = (str(paths[-1]), "raw")
                    self.assertIn(last_key, http_plumbing._STATIC_CACHE)

    def test_cli_ghost_export_uses_control_surface_shape(self) -> None:
        from codey.app import cli

        with tempfile.TemporaryDirectory() as td:
            args = mock.Mock(ghost_cmd="export", state_home=td)
            stdout = io.StringIO()
            with mock.patch("sys.stdout", stdout):
                code = cli.cmd_ghost(args)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(code, 0)
            self.assertTrue(payload["ok"])
            self.assertIn("inbox", payload)
            self.assertIn("hebbian", payload)
            self.assertIn("generated_at", payload)

    def test_cli_ghost_reset_and_delete_scope_via_control_surface(self) -> None:
        from codey.app import cli

        with tempfile.TemporaryDirectory() as td:
            args = mock.Mock(ghost_cmd="reset", state_home=td, yes=True)
            stdout = io.StringIO()
            with mock.patch("sys.stdout", stdout):
                code = cli.cmd_ghost(args)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(payload["action"], "reset_all")
            self.assertTrue(payload["ok"])

    def test_provider_availability_ttl_caches_cdp_scan(self) -> None:
        from codey.app import services as app_services

        app_services.reset_provider_availability_cache()
        with tempfile.TemporaryDirectory() as td:
            state = AppContext(td)
            try:
                calls: list[int] = []

                def fake_scan() -> dict[str, bool]:
                    calls.append(1)
                    return {"deepseek": True}

                with mock.patch.object(app_services, "provider_tab_availability", fake_scan):
                    first = app_services.provider_availability(state)
                    second = app_services.provider_availability(state)
            finally:
                state.close()
        self.assertTrue(first["deepseek"])
        self.assertTrue(second["deepseek"])
        self.assertEqual(len(calls), 1)
        app_services.reset_provider_availability_cache()

    def test_knowledge_read_notes_batches_single_roundtrip(self) -> None:
        from codey.knowledge.note import KnowledgeNote
        from codey.knowledge.store import KnowledgeStore

        with tempfile.TemporaryDirectory() as td:
            store = KnowledgeStore(td)
            try:
                for note_id in ("alpha", "beta"):
                    store.write_note(KnowledgeNote(
                        id=note_id, title=note_id, body=f"body {note_id}", type="fact",
                    ))
                with mock.patch.object(
                    store.index, "notes_by_ids", wraps=store.index.notes_by_ids
                ) as spy:
                    notes = store.read_notes(["alpha", "beta", "missing"])
            finally:
                store.close()
            self.assertIn("alpha", notes)
            self.assertIn("beta", notes)
            self.assertNotIn("missing", notes)
            self.assertEqual(spy.call_count, 1)

    def test_ledger_append_fast_path_avoids_full_rescan(self) -> None:
        from codey.runs.ledger import RunLedgerStore

        with tempfile.TemporaryDirectory() as td:
            writer = RunLedgerStore(td).open(
                run_id="r1", session_id="s1", project="p", task="t",
                provider="deepseek", mode="project",
            )
            with mock.patch(
                "codey.runs.ledger._ledger_file_state",
                side_effect=AssertionError("must use fast path"),
            ):
                writer.append("probe_event", note="fast")
            self.assertGreater(writer.seq, 0)


if __name__ == "__main__":
    unittest.main()
