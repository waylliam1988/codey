"""Recovery ownership regressions: snapshot, Changes GET, Restore, setup terminal.

Covers the first-batch boundaries without extra compat/fallback:
- null manifest entry must raise and preserve body
- capacity pre-check keeps disk clean; disk is the final guard
- cached capture_before still validates disk
- read-only Changes GET never deletes snapshots nor mutates cached tracker
- Restore holds the cross-process writer lease
- setup failures emit exactly one task_done
- worker stdin failure is submission_uncertain
- ledger middle corruption is unavailable
- npx dev-server gets merged explanation
"""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from codey.app.api import changes_response, restore_changes_response
from codey.app.context import AppContext
from codey.policies.shell_risk import classify_shell_risk
from codey.providers.diagnostics import FAILURE_SUBMISSION_UNCERTAIN, ProviderActionError
from codey.providers.worker import WorkerChatProvider, _PendingRequest, _WorkerSession
from codey.runs.ledger import read_ledger
from codey.storage.local_store import StoreCorruption
from codey.workspace.changes import ChangeTracker, SnapshotStore


def _provider() -> WorkerChatProvider:
    from types import SimpleNamespace

    provider = WorkerChatProvider.__new__(WorkerChatProvider)
    provider.provider_id = "qwen"
    provider.override = SimpleNamespace(root=Path("."), generation=1)
    provider.port = 19223
    provider.state_home = Path(".")
    provider.name = "qwen worker"
    provider.last_failure = None
    provider._life_lock = threading.Lock()
    provider._request_lock = threading.Lock()
    provider._session = None
    return provider


class SnapshotNullEntryTests(unittest.TestCase):
    def test_null_entry_raises_and_preserves_body(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as home:
            root = Path(td)
            (root / "a.py").write_text("ORIGINAL", encoding="utf-8")
            store = SnapshotStore(Path(home))
            self.assertEqual(store.put_baseline(root, "a.py", "ORIGINAL"), "ORIGINAL")
            body_path = store._baseline_path(root.resolve(), "a.py")
            self.assertEqual(body_path.read_text(encoding="utf-8"), "ORIGINAL")
            manifest = store.path_for(root.resolve())
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload["files"]["a.py"] = None
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(StoreCorruption):
                store.put_baseline(root, "a.py", "REPLACED")
            self.assertEqual(body_path.read_text(encoding="utf-8"), "ORIGINAL")

    def test_capacity_precheck_keeps_disk_clean(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as home:
            root = Path(td)
            store = SnapshotStore(Path(home))
            tracker = ChangeTracker(root, store)
            (root / "a.py").write_text("A", encoding="utf-8")
            tracker.capture_before("a.py")
            manifest = store.path_for(root.resolve())
            before_payload = json.loads(manifest.read_text(encoding="utf-8"))
            # Force memory to look full; disk must not gain the rejected entry.
            tracker._before["fill"] = "x"
            tracker._total_bytes = 0
            # Shrink the limit via patch to simulate 1-file cap.
            (root / "b.py").write_text("B", encoding="utf-8")
            with mock.patch(
                "codey.workspace.changes.MAX_SNAPSHOT_FILES", 1
            ), self.assertRaises(ValueError):
                tracker.capture_before("b.py")
            after_payload = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertNotIn("b.py", after_payload["files"])
            self.assertEqual(before_payload["files"], after_payload["files"])

    def test_disk_capacity_guard_inside_store(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as home:
            root = Path(td)
            store = SnapshotStore(Path(home))
            (root / "a.py").write_text("A", encoding="utf-8")
            store.put_baseline(root, "a.py", "A")
            (root / "b.py").write_text("B", encoding="utf-8")
            with mock.patch("codey.workspace.changes.MAX_SNAPSHOT_FILES", 1), self.assertRaises(ValueError):
                store.put_baseline(root, "b.py", "B")

    def test_cached_capture_validates_disk(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as home:
            root = Path(td)
            (root / "a.py").write_text("A", encoding="utf-8")
            store = SnapshotStore(Path(home))
            tracker = ChangeTracker(root, store)
            tracker.capture_before("a.py")
            manifest = store.path_for(root.resolve())
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload["files"]["a.py"] = {"baseline": "wrong.txt"}
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(StoreCorruption):
                tracker.capture_before("a.py")


class ChangesGetTests(unittest.TestCase):
    def test_git_mode_get_keeps_manifest_and_tracker(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as home:
            root = Path(td)
            (root / "a.py").write_text("A", encoding="utf-8")
            ctx = AppContext(Path(home))
            tracker = ctx.change_tracker_for(str(root), persistent=True)
            tracker.capture_before("a.py")
            manifest = ctx.snapshot_store.path_for(Path(str(root)).resolve())
            self.assertTrue(manifest.exists())
            old_store = tracker.store
            with mock.patch(
                "codey.app.api.is_git_repository", return_value=True
            ), mock.patch(
                "codey.workspace.changes.collect_git_changes",
                return_value={"ok": True, "mode": "git", "files": []},
            ):
                status, _ = changes_response(ctx, str(root))
            self.assertEqual(status, 200)
            self.assertTrue(manifest.exists())
            self.assertIs(tracker.store, old_store)


class RestoreLeaseTests(unittest.TestCase):
    def test_restore_holds_writer_lease(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as home:
            root = Path(td)
            (root / "a.py").write_text("A", encoding="utf-8")
            home_path = Path(home)
            ctx_a = AppContext(home_path)
            ctx_b = AppContext(home_path)
            tracker = ctx_a.change_tracker_for(str(root), persistent=True)
            tracker.capture_before("a.py")
            (root / "a.py").write_text("B", encoding="utf-8")
            tracker.capture_after("a.py")
            self.assertTrue(ctx_a.acquire_project_writer(str(root)))
            try:
                status, _ = restore_changes_response(
                    ctx_b, {"project": str(root)}
                )
                self.assertEqual(status, 409)
                self.assertEqual(
                    (root / "a.py").read_text(encoding="utf-8"), "B"
                )
            finally:
                ctx_a.release_project_writer(str(root))
            status, _ = restore_changes_response(ctx_b, {"project": str(root)})
            self.assertEqual(status, 200)
            self.assertEqual((root / "a.py").read_text(encoding="utf-8"), "A")


class SetupTerminalTests(unittest.TestCase):
    def test_busy_setup_emits_single_task_done(self) -> None:
        from codey.operations.task_run import _setup_run_state
        from codey.task.model import TaskSubmission

        with tempfile.TemporaryDirectory() as home:
            ctx = AppContext(Path(home))
            events: list[dict] = []
            ctx.emit = events.append  # type: ignore[method-assign]
            with tempfile.TemporaryDirectory() as td:
                request = TaskSubmission(
                    session_id="s1", project=str(td), task="t",
                    max_turns=2, continue_task=False,
                    provider_id="qwen", intent="project",
                )
                # Reserve first so second setup hits project_write_busy via lease.
                self.assertTrue(ctx.acquire_project_writer(str(td)))
                # Force needs_writer path with a fake non-git project.
                from codey.operations.task_run import TaskRunDeps

                deps = TaskRunDeps(
                    state=ctx,  # type: ignore[arg-type]
                    agent_run=lambda *a, **k: None,
                    collect_changes=lambda *a, **k: {},
                    run_review=lambda *a, **k: None,
                    capture_provider_failure=lambda *a, **k: None,
                    workspace_revisions=mock.Mock(),
                    is_git_repository=lambda p: False,
                )
                # First reservation happens inside setup; hold the writer so
                # setup sees busy.
                setup, outcome = _setup_run_state(deps, request)
                self.assertIsNone(setup)
                self.assertIsNotNone(outcome)
                dones = [e for e in events if e.get("type") == "task_done"]
                self.assertEqual(len(dones), 1)
                self.assertIsNone(ctx.current_run())
                # Writer still held by us; release then reacquire works.
                ctx.release_project_writer(str(td))
                self.assertTrue(ctx.acquire_project_writer(str(td)))
                ctx.release_project_writer(str(td))


class WorkerUncertainTests(unittest.TestCase):
    def test_write_failure_is_uncertain(self) -> None:
        provider = _provider()
        proc = mock.Mock()
        proc.poll.return_value = None
        proc.stdin = mock.Mock()
        session = _WorkerSession(proc=proc, job=None)
        provider._session = session
        pending = _PendingRequest(request_id="r1", method="send")
        with session.lock:
            session.pending = pending
            session.write_error = "provider worker stdin is unavailable: broken"
            session.write_done.set()
        with self.assertRaises(ProviderActionError) as raised:
            provider._await_write(session, pending, time.monotonic() + 5.0)
        self.assertEqual(
            raised.exception.failure.kind, FAILURE_SUBMISSION_UNCERTAIN
        )


class LedgerCorruptionTests(unittest.TestCase):
    def test_middle_bad_line_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "run.jsonl"
            path.write_text(
                '{"schema_version": 1, "seq": 1, "type": "run_started"}\n'
                "NOT JSON\n"
                '{"schema_version": 1, "seq": 2, "type": "run_finished"}\n',
                encoding="utf-8",
            )
            self.assertEqual(read_ledger(path), [])


class ShellNpxTests(unittest.TestCase):
    def test_npx_vite_dev_is_merged_dev_server(self) -> None:
        risk = classify_shell_risk("npx vite dev")
        self.assertEqual(risk.label, "dev_server")
        self.assertIn("download", risk.detail.lower())
        risk2 = classify_shell_risk("npx next dev")
        self.assertEqual(risk2.label, "dev_server")

    def test_npx_create_stays_install(self) -> None:
        risk = classify_shell_risk("npx create-vite my-app")
        self.assertEqual(risk.label, "dependency_install")


if __name__ == "__main__":
    unittest.main()
