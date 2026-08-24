from __future__ import annotations

import tempfile
import unittest
import subprocess
import sys
from pathlib import Path
from unittest import mock

from codey import agent
from codey import changes
from codey.changes import ChangeTracker, SnapshotStore


class FakeProvider:
    name = "Fake Provider"
    location = "fake://provider"

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)

    def new_chat(self) -> None:
        pass

    def send(self, _text: str, timeout: float | None = None) -> str:
        del timeout
        return self.replies.pop(0)

    def close(self) -> None:
        pass


class ChangeTrackerTests(unittest.TestCase):
    def test_snapshot_and_untracked_diffs_have_no_double_blank_lines(self) -> None:
        # Regression: keepends=True fed into unified_diff(lineterm="") plus a
        # "\n".join rendered one blank line after every content line for
        # snapshot-mode (non-git) projects.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tracker = ChangeTracker(root)

            (root / "app.py").write_text("a\nb\nc\n", encoding="utf-8")
            tracker.capture_before("app.py")
            (root / "app.py").write_text("a\nB\nc\n", encoding="utf-8")
            data = tracker.collect()

            self.assertNotIn("\n\n", data["diff"])
            self.assertIn("@@ -1,3 +1,3 @@\n a\n-b\n+B\n c", data["diff"])

            new_file = root / "untracked.py"
            new_file.write_text("x\ny\n", encoding="utf-8")
            untracked = changes._untracked_file_diff(root, "untracked.py")

            self.assertIsNotNone(untracked)
            body, line_count = untracked
            self.assertNotIn("\n\n", body)
            self.assertEqual(line_count, 2)

    def test_collects_new_file_snapshot_diff(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tracker = ChangeTracker(root)

            tracker.capture_before("app.py")
            (root / "app.py").write_text("print('ok')\n", encoding="utf-8")
            data = tracker.collect()

            self.assertTrue(data["ok"], data)
            self.assertEqual(data["mode"], "snapshot")
            self.assertEqual(data["changed_count"], 1)
            self.assertEqual(data["files"][0]["status"], "A")
            self.assertEqual(data["files"][0]["additions"], 1)
            self.assertIn("diff --git a/app.py b/app.py", data["diff"])
            self.assertIn("--- /dev/null", data["diff"])
            self.assertIn("+++ b/app.py", data["diff"])
            self.assertIn("+print('ok')", data["diff"])

    def test_collects_modified_file_snapshot_diff(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "app.py"
            path.write_text("old\nsame\n", encoding="utf-8")
            tracker = ChangeTracker(root)

            tracker.capture_before("app.py")
            path.write_text("new\nsame\nextra\n", encoding="utf-8")
            data = tracker.collect()

            self.assertEqual(data["changed_count"], 1)
            self.assertEqual(data["files"][0]["status"], "M")
            self.assertIn("diff --git a/app.py b/app.py", data["diff"])
            self.assertIn("-old", data["diff"])
            self.assertIn("+new", data["diff"])
            self.assertIn("+extra", data["diff"])

    def test_restore_reverts_modified_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "app.py"
            path.write_text("old\n", encoding="utf-8")
            tracker = ChangeTracker(root)
            tracker.capture_before("app.py")
            path.write_text("new\n", encoding="utf-8")
            tracker.capture_after("app.py")

            result = tracker.restore()

            self.assertTrue(result.ok, result)
            self.assertEqual(result.restored, ["app.py"])
            self.assertEqual(path.read_text(encoding="utf-8"), "old\n")
            self.assertEqual(tracker.collect()["changed_count"], 0)

    def test_restore_removes_new_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "app.py"
            tracker = ChangeTracker(root)
            tracker.capture_before("app.py")
            path.write_text("new\n", encoding="utf-8")
            tracker.capture_after("app.py")

            result = tracker.restore()

            self.assertTrue(result.ok, result)
            self.assertFalse(path.exists())

    def test_restore_conflicts_when_file_changed_after_snapshot_diff(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "app.py"
            path.write_text("old\n", encoding="utf-8")
            tracker = ChangeTracker(root)
            tracker.capture_before("app.py")
            path.write_text("new\n", encoding="utf-8")
            tracker.collect()
            path.write_text("manual\n", encoding="utf-8")

            result = tracker.restore()

            self.assertFalse(result.ok)
            self.assertEqual(result.conflicts, ["app.py"])
            self.assertEqual(path.read_text(encoding="utf-8"), "manual\n")

    def test_capture_rejects_paths_outside_project(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tracker = ChangeTracker(td)

            with self.assertRaisesRegex(ValueError, "escapes project root"):
                tracker.capture_before("../escape.py")

    def test_persistent_tracker_restores_modified_file_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
            root = Path(td)
            path = root / "app.py"
            path.write_text("old\n", encoding="utf-8")
            store = SnapshotStore(state_td)
            first = ChangeTracker(root, store)
            first.capture_before("app.py")
            path.write_text("new\n", encoding="utf-8")
            first.capture_after("app.py")

            restarted = ChangeTracker(root, store)
            changes = restarted.collect()
            result = restarted.restore()

            self.assertEqual(changes["changed_count"], 1)
            self.assertTrue(result.ok, result)
            self.assertEqual(path.read_text(encoding="utf-8"), "old\n")
            self.assertFalse(store.path_for(root).exists())

    def test_persistent_tracker_removes_new_file_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
            root = Path(td)
            path = root / "new.py"
            store = SnapshotStore(state_td)
            first = ChangeTracker(root, store)
            first.capture_before("new.py")
            path.write_text("created\n", encoding="utf-8")
            first.capture_after("new.py")

            restarted = ChangeTracker(root, store)
            result = restarted.restore()

            self.assertTrue(result.ok, result)
            self.assertFalse(path.exists())

    def test_refresh_does_not_accept_manual_edit_as_expected_after_state(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
            root = Path(td)
            path = root / "app.py"
            path.write_text("old\n", encoding="utf-8")
            store = SnapshotStore(state_td)
            first = ChangeTracker(root, store)
            first.capture_before("app.py")
            path.write_text("codey\n", encoding="utf-8")
            first.capture_after("app.py")
            path.write_text("manual\n", encoding="utf-8")

            restarted = ChangeTracker(root, store)
            restarted.collect()
            result = restarted.restore()

            self.assertFalse(result.ok)
            self.assertEqual(result.conflicts, ["app.py"])
            self.assertEqual(path.read_text(encoding="utf-8"), "manual\n")

    def test_missing_after_hash_never_accepts_current_content_during_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
            root = Path(td)
            path = root / "app.py"
            path.write_text("old\n", encoding="utf-8")
            store = SnapshotStore(state_td)
            first = ChangeTracker(root, store)
            first.capture_before("app.py")
            path.write_text("partial Codey write\n", encoding="utf-8")

            path.write_text("manual after restart\n", encoding="utf-8")
            restarted = ChangeTracker(root, store)
            restarted.collect()
            result = restarted.restore()

            self.assertFalse(result.ok)
            self.assertEqual(result.conflicts, ["app.py"])
            self.assertEqual(
                path.read_text(encoding="utf-8"),
                "manual after restart\n",
            )

    def test_snapshot_save_failure_prevents_agent_write(self) -> None:
        write = '{"tool":"edit","args":{"path":"app.py","content":"new\\n"}}'
        done = '{"tool":"done","args":{"summary":"finished"}}'
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
            root = Path(td)
            path = root / "app.py"
            path.write_text("old\n", encoding="utf-8")
            store = SnapshotStore(state_td)
            tracker = ChangeTracker(root, store)

            with mock.patch.object(store, "save", side_effect=OSError("disk full")):
                agent.run(
                    FakeProvider(write, done),
                    root,
                    "update app",
                    fresh_chat=False,
                    change_tracker=tracker,
                    on_event=lambda _event: None,
                )

            self.assertEqual(path.read_text(encoding="utf-8"), "old\n")
            self.assertFalse(tracker.has_snapshots)

    def test_snapshot_capacity_limits_are_enforced_before_capture(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
            root = Path(td)
            (root / "a.py").write_text("a", encoding="utf-8")
            (root / "b.py").write_text("bbb", encoding="utf-8")
            tracker = ChangeTracker(root, SnapshotStore(state_td))

            with mock.patch("codey.changes.MAX_SNAPSHOT_FILES", 1):
                tracker.capture_before("a.py")
                with self.assertRaisesRegex(ValueError, "file limit"):
                    tracker.capture_before("b.py")

            other = ChangeTracker(root, SnapshotStore(Path(state_td) / "other"))
            with mock.patch("codey.changes.MAX_SNAPSHOT_TOTAL_BYTES", 2):
                with self.assertRaisesRegex(ValueError, "size limit"):
                    other.capture_before("b.py")

    def test_binary_file_without_text_baseline_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
            root = Path(td)
            path = root / "image.bin"
            path.write_bytes(b"\xff\xfe\x00")
            tracker = ChangeTracker(root, SnapshotStore(state_td))

            with self.assertRaises(UnicodeDecodeError):
                tracker.capture_before("image.bin")

            self.assertFalse(tracker.has_snapshots)
            self.assertEqual(path.read_bytes(), b"\xff\xfe\x00")

    def test_unreadable_after_state_does_not_discard_recovery_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
            root = Path(td)
            path = root / "new.py"
            store = SnapshotStore(state_td)
            tracker = ChangeTracker(root, store)
            tracker.capture_before("new.py")
            path.write_text("large", encoding="utf-8")
            tracker.capture_after("new.py")

            with mock.patch("codey.changes.MAX_SNAPSHOT_FILE_BYTES", 2):
                changes = tracker.collect()

            self.assertEqual(changes["changed_count"], 0)
            self.assertTrue(tracker.has_snapshots)
            self.assertTrue(store.path_for(root).is_file())

    def test_corrupt_or_escaping_snapshot_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
            root = Path(td)
            store = SnapshotStore(state_td)
            path = store.path_for(root)
            path.parent.mkdir(parents=True)
            path.write_text(
                '{"schema_version":1,"before":{"../outside.py":"x"},"after_hashes":{}}',
                encoding="utf-8",
            )

            tracker = ChangeTracker(root, store)

            self.assertFalse(tracker.has_snapshots)

    def test_snapshot_survives_abrupt_process_exit(self) -> None:
        script = (
            "import os,sys; from pathlib import Path; "
            "from codey.changes import ChangeTracker,SnapshotStore; "
            "root=Path(sys.argv[1]); store=SnapshotStore(sys.argv[2]); "
            "tracker=ChangeTracker(root,store); tracker.capture_before('app.py'); "
            "(root/'app.py').write_text('new\\n',encoding='utf-8'); "
            "tracker.capture_after('app.py'); os._exit(0)"
        )
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
            root = Path(td)
            path = root / "app.py"
            path.write_text("old\n", encoding="utf-8")

            proc = subprocess.run(
                [sys.executable, "-B", "-c", script, str(root), state_td],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
                check=False,
            )
            restarted = ChangeTracker(root, SnapshotStore(state_td))
            result = restarted.restore()

            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(result.ok, result)
            self.assertEqual(path.read_text(encoding="utf-8"), "old\n")


if __name__ == "__main__":
    unittest.main()
