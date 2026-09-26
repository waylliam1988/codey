"""Boundary hardening for snapshot, ledger, Ghost, UI, and terminal paths.

Covers the exact regressions fixed in this round: no prefix projection,
no manifest rebuild from the working file, no cross-project continuity,
explicit UI conflicts, and visible terminal failures.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codey.app import api as app_api
from codey.app import context as app_context
from codey.storage.local_store import StoreCorruption


class SnapshotRequireBaselineTests(unittest.TestCase):
    def _tracker(self, root: Path, home: Path):
        ctx = app_context.AppContext(home)
        tracker = ctx.change_tracker_for(str(root), persistent=True)
        return ctx, tracker

    def test_cached_capture_with_missing_manifest_raises_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as home:
            root = Path(td)
            (root / "a.py").write_text("A", encoding="utf-8")
            home_path = Path(home)
            ctx, tracker = self._tracker(root, home_path)
            tracker.capture_before("a.py")
            manifest = ctx.snapshot_store.path_for(root.resolve())
            body_dir = manifest.parent / "baselines"
            bodies_before = sorted(p.name for p in body_dir.glob("*.txt")) if body_dir.exists() else []
            self.assertTrue(manifest.exists())
            manifest.unlink()
            (root / "a.py").write_text("B", encoding="utf-8")
            with self.assertRaises(StoreCorruption):
                tracker.capture_before("a.py")
            self.assertFalse(manifest.exists())
            if body_dir.exists():
                self.assertEqual(sorted(p.name for p in body_dir.glob("*.txt")), bodies_before)
            self.assertEqual((root / "a.py").read_text(encoding="utf-8"), "B")

    def test_cached_capture_with_missing_entry_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as home:
            root = Path(td)
            (root / "a.py").write_text("A", encoding="utf-8")
            (root / "b.py").write_text("X", encoding="utf-8")
            home_path = Path(home)
            ctx, tracker = self._tracker(root, home_path)
            tracker.capture_before("a.py")
            tracker.capture_before("b.py")
            manifest = ctx.snapshot_store.path_for(root.resolve())
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            del payload["files"]["a.py"]
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            (root / "a.py").write_text("B", encoding="utf-8")
            with self.assertRaises(StoreCorruption):
                tracker.capture_before("a.py")
            # b.py entry still intact and working file untouched.
            after = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertIn("b.py", after["files"])
            self.assertEqual((root / "a.py").read_text(encoding="utf-8"), "B")

    def test_cached_capture_with_drifted_disk_baseline_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as home:
            root = Path(td)
            (root / "a.py").write_text("A", encoding="utf-8")
            home_path = Path(home)
            ctx, tracker = self._tracker(root, home_path)
            tracker.capture_before("a.py")
            manifest = ctx.snapshot_store.path_for(root.resolve())
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            body_name = payload["files"]["a.py"]["baseline"]
            body_path = manifest.parent / "baselines" / body_name
            body_path.write_text("ATTACKER", encoding="utf-8")
            with self.assertRaises(StoreCorruption):
                tracker.capture_before("a.py")
            self.assertEqual((root / "a.py").read_text(encoding="utf-8"), "A")

    def test_missing_old_body_blocks_new_baseline_and_reports_capacity(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as home:
            root = Path(td)
            (root / "a.py").write_text("A", encoding="utf-8")
            home_path = Path(home)
            ctx, tracker = self._tracker(root, home_path)
            tracker.capture_before("a.py")
            manifest = ctx.snapshot_store.path_for(root.resolve())
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            body_name = payload["files"]["a.py"]["baseline"]
            (manifest.parent / "baselines" / body_name).unlink()
            (root / "b.py").write_text("B", encoding="utf-8")
            with self.assertRaises(StoreCorruption):
                tracker.capture_before("b.py")
            after = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertNotIn("b.py", after["files"])


class LedgerStrictTests(unittest.TestCase):
    def _writer(self, path: Path):
        from codey.runs.ledger import RunLedgerStore

        store = RunLedgerStore(path.parent)
        # Bypass session sharding for a direct file writer.
        from codey.runs.ledger import RunLedgerWriter

        return RunLedgerWriter(path, run_id="r", session_id="s"), store

    def test_bad_middle_row_yields_no_projection(self) -> None:
        from codey.runs.ledger import read_ledger

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "run.jsonl"
            writer, _ = self._writer(path)
            writer.append("info", text="first")
            with path.open("a", encoding="utf-8") as handle:
                handle.write("not-json\n")
            writer2_path = path
            # Append a valid row after corruption via raw write (simulates splice).
            from codey.runs.ledger import _event_common, _json_line

            with writer2_path.open("a", encoding="utf-8") as handle:
                handle.write(_json_line({**_event_common("r", "s", 99, "info"), "text": "spliced"}))
            self.assertEqual(read_ledger(path), [])

    def test_bad_tail_row_yields_no_projection(self) -> None:
        from codey.runs.ledger import read_ledger

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "run.jsonl"
            writer, _ = self._writer(path)
            writer.append("info", text="first")
            with path.open("a", encoding="utf-8") as handle:
                handle.write("torn-{")
            self.assertEqual(read_ledger(path), [])

    def test_reopen_corrupt_file_refuses_append(self) -> None:
        from codey.runs.ledger import LedgerWriteFailed, RunLedgerWriter, read_ledger

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "run.jsonl"
            writer = RunLedgerWriter(path, run_id="r", session_id="s")
            writer.append("info", text="first")
            with path.open("a", encoding="utf-8") as handle:
                handle.write("bad-tail\n")
            reopened = RunLedgerWriter(path, run_id="r", session_id="s")
            self.assertTrue(reopened.disabled)
            with self.assertRaises(LedgerWriteFailed):
                reopened.append("info", text="second")
            with self.assertRaises(LedgerWriteFailed):
                reopened.finish(summary="x", stop_reason="done", turns=1, max_turns=2, provider="p")
            self.assertEqual(read_ledger(path), [])

    def test_ghost_ignores_incomplete_projection_with_warning(self) -> None:
        from codey.operations.ghost_post_turn import GhostTaskPolicyDeps, _run_projection
        from codey.runs.ledger import RunLedgerStore

        with tempfile.TemporaryDirectory() as td:
            store = RunLedgerStore(td)
            writer = store.open(run_id="r1", session_id="s1", project="E:/p", task="t", provider="p", mode="m")
            writer.append("info", text="first")
            # Corrupt the tail: Ghost must see no projection.
            path = store.path_for("s1", "r1")
            with path.open("a", encoding="utf-8") as handle:
                handle.write("bad\n")
            warnings: list[dict] = []
            state = mock.Mock()
            state.emit = warnings.append
            deps = GhostTaskPolicyDeps(state=state, run_ledgers=store)
            self.assertIsNone(_run_projection(deps, "s1", "r1"))
            self.assertTrue(any(w.get("type") == "ghost_post_turn_warning" for w in warnings))

    def test_ghost_ignores_unfinished_but_valid_prefix(self) -> None:
        from codey.operations.ghost_post_turn import GhostTaskPolicyDeps, _run_projection
        from codey.runs.ledger import RunLedgerStore

        with tempfile.TemporaryDirectory() as td:
            store = RunLedgerStore(td)
            store.open(run_id="r2", session_id="s2", project="E:/p", task="t", provider="p", mode="m")
            warnings: list[dict] = []
            state = mock.Mock()
            state.emit = warnings.append
            deps = GhostTaskPolicyDeps(state=state, run_ledgers=store)
            # Valid prefix without run_finished is not complete.
            self.assertIsNone(_run_projection(deps, "s2", "r2"))
            self.assertTrue(any(w.get("type") == "ghost_post_turn_warning" for w in warnings))


class GhostKnowledgeScopeTests(unittest.TestCase):
    def test_project_query_failure_yields_no_items(self) -> None:
        from codey.ghost.continuity import _items_from_knowledge

        class BadIndex:
            def recent(self, *args, **kwargs):
                raise OSError("index down")

        store = mock.Mock()
        store.index = BadIndex()
        warnings: list[str] = []
        out = _items_from_knowledge(store, now="2026-01-01T00:00:00Z", session_id="s1", project="E:/p1", warnings=warnings)
        self.assertEqual(out, [])
        self.assertIn("knowledge_unreadable", warnings)

    def test_foreign_project_note_is_skipped(self) -> None:
        from codey.ghost.continuity import _items_from_knowledge

        foreign = {
            "id": "n-foreign",
            "project": "E:/other",
            "session_id": "s-other",
            "title": "Foreign focus",
            "type": "synthesis",
            "updated": "2026-01-01T00:00:00Z",
        }
        store = mock.Mock()
        store.index.recent.return_value = [foreign]
        warnings: list[str] = []
        out = _items_from_knowledge(store, now="2026-01-01T00:00:00Z", session_id="s1", project="E:/p1", warnings=warnings)
        self.assertEqual(out, [])
        self.assertIn("knowledge_scope_mismatch", warnings)
        # Old wide fallback must be gone: single recent call with project filter.
        self.assertEqual(store.index.recent.call_count, 1)
        _, kwargs = store.index.recent.call_args
        self.assertIn("project", kwargs)


class UiStateConflictApiTests(unittest.TestCase):
    def test_same_base_different_content_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ctx = app_context.AppContext(Path(td))
            first = {"active_id": "a", "updated_at": 1, "revision": 0, "sessions": [], "projects": []}
            status, body = app_api.save_ui_state_response(ctx, {"state": first, "base_revision": 0})
            self.assertEqual(status, 200)
            rev = body["revision"]
            other = {"active_id": "b", "updated_at": 1, "revision": 0, "sessions": [], "projects": []}
            status, body = app_api.save_ui_state_response(ctx, {"state": other, "base_revision": 0})
            self.assertEqual(status, 409)
            self.assertEqual(body["revision"], rev)
            self.assertEqual(ctx.load_ui_state()["active_id"], "a")

    def test_interleaved_pages_second_writer_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ctx = app_context.AppContext(Path(td))
            base = int(ctx.load_ui_state().get("revision") or 0)
            page_a = {"active_id": "a", "updated_at": 1, "revision": 0, "sessions": [], "projects": []}
            page_b = {"active_id": "b", "updated_at": 1, "revision": 0, "sessions": [], "projects": []}
            status_a, body_a = app_api.save_ui_state_response(ctx, {"state": page_a, "base_revision": base})
            self.assertEqual(status_a, 200)
            status_b, body_b = app_api.save_ui_state_response(ctx, {"state": page_b, "base_revision": base})
            self.assertEqual(status_b, 409)
            self.assertEqual(body_b["revision"], body_a["revision"])

    def test_duplicate_same_content_succeeds_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ctx = app_context.AppContext(Path(td))
            payload = {"active_id": "a", "updated_at": 1, "revision": 0, "sessions": [], "projects": []}
            status1, body1 = app_api.save_ui_state_response(ctx, {"state": payload, "base_revision": 0})
            self.assertEqual(status1, 200)
            status2, body2 = app_api.save_ui_state_response(ctx, {"state": payload, "base_revision": 0})
            self.assertEqual(status2, 200)
            self.assertEqual(body2["revision"], body1["revision"])

    def test_second_server_instance_is_rejected(self) -> None:
        from pathlib import Path as _Path

        from codey.storage.file_lock import LockTimeout, acquire_lease

        with tempfile.TemporaryDirectory() as td:
            lock_path = _Path(td) / ".server.lock"
            first = acquire_lease(lock_path, timeout_seconds=0.0)
            try:
                with self.assertRaises(LockTimeout):
                    acquire_lease(lock_path, timeout_seconds=0.0)
            finally:
                first.release()
            # After release a new owner can claim it.
            second = acquire_lease(lock_path, timeout_seconds=0.0)
            second.release()


class TerminalAndLedgerBoundaryTests(unittest.TestCase):
    def test_setup_failure_finish_false_releases_slot_without_second_event(self) -> None:
        from codey.operations.task_run import _finish_setup_failure
        from codey.task.model import TaskSubmission

        state = mock.Mock()
        state.finish_run.return_value = False
        request = TaskSubmission(session_id="s", project=None, task="t", max_turns=2, continue_task=False, provider_id="p", intent="chat")
        outcome = _finish_setup_failure(state, request, "run-1", "boom", outcome_reason="setup_failed", task_kind="chat")
        self.assertEqual(outcome.status, "failed")
        state.finish_run.assert_called_once()
        state.release_run.assert_called_once_with("run-1")

    def test_setup_failure_finish_raises_releases_slot(self) -> None:
        from codey.operations.task_run import _finish_setup_failure
        from codey.task.model import TaskSubmission

        state = mock.Mock()
        state.finish_run.side_effect = RuntimeError("emit down")
        request = TaskSubmission(session_id="s", project=None, task="t", max_turns=2, continue_task=False, provider_id="p", intent="chat")
        outcome = _finish_setup_failure(state, request, "run-1", "boom", outcome_reason="setup_failed", task_kind="chat")
        self.assertEqual(outcome.status, "failed")
        state.release_run.assert_called_once_with("run-1")

    def test_append_ledger_only_downgrades_expected_storage_faults(self) -> None:
        from codey.operations.task_phases.hooks import build_hooks
        from codey.runs.ledger import LedgerWriteFailed

        work = mock.Mock()
        work.ledger = mock.Mock()
        work.trace = None
        work.turns_observed = 0
        work.record_agent_events_in_ledger = False
        work.recent_events = []
        work.evidence = mock.Mock()
        work.evidence.record.return_value = None
        state = mock.Mock()
        state.providers.supervisor = None
        state.emit.return_value = None
        deps = mock.Mock()
        deps.work_checkpoints = None
        deps.state = state
        hooks = build_hooks(
            deps, state, work, session_id="s", run_id="r", project=None,
            max_turns=2, project_config_ignored=(), review_log_lines=10,
            project_completion_deps=mock.Mock(),
        )
        work.ledger.append.side_effect = LedgerWriteFailed("no disk")
        hooks.append_ledger(lambda ledger: ledger.append("info", text="x"))
        self.assertIsNone(work.ledger)
        # Unexpected programming errors must surface, not become "ledger unavailable".
        work.ledger = mock.Mock()
        work.ledger.append.side_effect = TypeError("bad call")
        with self.assertRaises(TypeError):
            hooks.append_ledger(lambda ledger: ledger.append("info", text="x"))

    def test_conversation_delete_serializes_with_save(self) -> None:
        from codey.storage.conversation_store import ConversationStore

        with tempfile.TemporaryDirectory() as td:
            store = ConversationStore(td)
            import codey.storage.conversation_store as _mod

            with mock.patch.object(_mod, "with_file_lock", wraps=_mod.with_file_lock) as lock_spy:
                from codey.agents.handoff import ConversationContext

                store.save("s1", ConversationContext())
                store.delete("s1")
            # Both paths hold the same directory lock; delete never bypasses it.
            targets = [call.args[0] if call.args else call.kwargs.get("path") for call in lock_spy.call_args_list]
            self.assertGreaterEqual(len(targets), 2)
            self.assertTrue(all(str(t) == str(store.directory) for t in targets if t is not None))


class ReadFileLineBoundaryTests(unittest.TestCase):
    def test_vertical_tab_is_not_a_line_break(self) -> None:
        from codey.toolchain.runtime import read_file

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "a.txt").write_text("a\vb", encoding="utf-8")
            outcome = read_file(root, "a.txt", offset=2, limit=5)
            self.assertFalse(outcome.ok)
            self.assertIn("exceeds", str(outcome.model_text if hasattr(outcome, "model_text") else outcome))


if __name__ == "__main__":
    unittest.main()
