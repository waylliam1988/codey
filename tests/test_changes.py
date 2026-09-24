from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from codey.agents import runner as agent
from codey.agents.request import AgentRequest
from codey.workspace import changes
from codey.workspace.changes import ChangeTracker, SnapshotStore


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
            self.assertIn("diff --git a/untracked.py b/untracked.py", body)
            self.assertEqual(line_count, 2)

    def test_git_commands_disable_quoted_paths_for_cjk_status(self) -> None:
        from types import SimpleNamespace

        with mock.patch("codey.runtime.core.cancellation.run_process") as run:
            run.return_value = SimpleNamespace(
                args=[], returncode=0, stdout="", stderr="",
                stdout_truncated=False, stderr_truncated=False,
            )
            changes._run_git(Path("C:/project"), ["status", "--short"])

        argv = run.call_args.args[0]
        self.assertEqual(argv[:4], ["git", "-c", "core.quotePath=false", "-C"])

    def test_collect_git_changes_skips_diff_commands_when_status_is_clean(self) -> None:
        from types import SimpleNamespace

        def _proc(args: list[str], stdout: str = "") -> SimpleNamespace:
            return SimpleNamespace(
                args=args, returncode=0, stdout=stdout, stderr="",
                stdout_truncated=False, stderr_truncated=False,
            )

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            def run_git(_cwd: Path, args: list[str]) -> SimpleNamespace:
                if args == ["rev-parse", "--show-toplevel"]:
                    return _proc(args, str(root))
                if args == ["status", "--short"]:
                    return _proc(args, "")
                raise AssertionError(f"unexpected git command: {args}")

            with mock.patch.object(changes, "_run_git", side_effect=run_git) as run:
                data = changes.collect_git_changes(root)

        self.assertTrue(data["ok"])
        self.assertEqual(data["changed_count"], 0)
        self.assertEqual(data["diff"], "")
        self.assertEqual(
            [call.args[1] for call in run.call_args_list],
            [["rev-parse", "--show-toplevel"], ["status", "--short"]],
        )

    def test_truncated_status_is_explicit_error_not_partial(self) -> None:
        from types import SimpleNamespace

        def _proc(args: list[str], stdout: str = "", truncated: bool = False) -> SimpleNamespace:
            return SimpleNamespace(
                args=args, returncode=0, stdout=stdout, stderr="",
                stdout_truncated=truncated, stderr_truncated=False,
            )

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            def run_git(_cwd: Path, args: list[str]) -> SimpleNamespace:
                if args == ["rev-parse", "--show-toplevel"]:
                    return _proc(args, str(root))
                if args == ["status", "--short"]:
                    return _proc(args, " M a.py\n", truncated=True)
                raise AssertionError(f"unexpected git command: {args}")

            with mock.patch.object(changes, "_run_git", side_effect=run_git):
                data = changes.collect_git_changes(root)

        self.assertFalse(data["ok"])
        self.assertIn("truncated", data["error"])

    def test_capture_truncated_diff_is_marked_distinctly(self) -> None:
        from types import SimpleNamespace

        def _proc(args: list[str], stdout: str = "", truncated: bool = False) -> SimpleNamespace:
            return SimpleNamespace(
                args=args, returncode=0, stdout=stdout, stderr="",
                stdout_truncated=truncated, stderr_truncated=False,
            )

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            def run_git(_cwd: Path, args: list[str]) -> SimpleNamespace:
                if args == ["rev-parse", "--show-toplevel"]:
                    return _proc(args, str(root))
                if args == ["status", "--short"]:
                    return _proc(args, " M a.py\n")
                if args == ["diff", "--numstat"]:
                    return _proc(args, "1\t0\ta.py\n")
                if args == ["diff", "--cached", "--numstat"]:
                    return _proc(args, "")
                if args == ["diff", "--no-ext-diff", "--"]:
                    return _proc(args, "diff --git a/a.py b/a.py\n+x\n", truncated=True)
                if args == ["diff", "--cached", "--no-ext-diff", "--"]:
                    return _proc(args, "")
                raise AssertionError(f"unexpected git command: {args}")

            with mock.patch.object(changes, "_run_git", side_effect=run_git):
                data = changes.collect_git_changes(root)

        self.assertTrue(data["ok"])
        self.assertTrue(data["truncated"])
        self.assertIn("capture truncated", data["diff"])

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

            with mock.patch.object(store, "put_baseline", side_effect=OSError("disk full")):
                agent.run(AgentRequest(
                    provider=FakeProvider(write, done),
                    project=root,
                    task="update app",
                    fresh_chat=False,
                    change_tracker=tracker,
                    on_event=lambda _event: None,
                ))

            self.assertEqual(path.read_text(encoding="utf-8"), "old\n")
            self.assertFalse(tracker.has_snapshots)

    def test_snapshot_capacity_limits_are_enforced_before_capture(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
            root = Path(td)
            (root / "a.py").write_text("a", encoding="utf-8")
            (root / "b.py").write_text("bbb", encoding="utf-8")
            tracker = ChangeTracker(root, SnapshotStore(state_td))

            with mock.patch("codey.workspace.changes.MAX_SNAPSHOT_FILES", 1):
                tracker.capture_before("a.py")
                with self.assertRaisesRegex(ValueError, "file limit"):
                    tracker.capture_before("b.py")

            other = ChangeTracker(root, SnapshotStore(Path(state_td) / "other"))
            with mock.patch("codey.workspace.changes.MAX_SNAPSHOT_TOTAL_BYTES", 2), self.assertRaisesRegex(ValueError, "size limit"):
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

            with mock.patch("codey.workspace.changes.MAX_SNAPSHOT_FILE_BYTES", 2):
                changes = tracker.collect()

            self.assertEqual(changes["changed_count"], 0)
            self.assertTrue(tracker.has_snapshots)
            self.assertTrue(store.path_for(root).is_file())

    def test_collect_is_read_only_and_prune_clean_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
            root = Path(td)
            clean_path = root / "clean.py"
            dirty_path = root / "dirty.py"
            clean_path.write_text("same\n", encoding="utf-8")
            store = SnapshotStore(state_td)
            tracker = ChangeTracker(root, store)
            tracker.capture_before("clean.py")
            tracker.capture_before("dirty.py")
            dirty_path.write_text("edited\n", encoding="utf-8")
            manifest = store.path_for(root)

            # UI polling must never mutate recovery state.
            first = tracker.collect()
            self.assertEqual(first["changed_count"], 1)
            self.assertTrue(manifest.is_file())
            self.assertTrue(store.dir_for(root).is_dir())

            pruned = tracker.prune_clean()
            self.assertEqual(pruned, ["clean.py"])
            self.assertTrue(tracker.has_snapshots)
            restarted = ChangeTracker(root, store)
            self.assertEqual(restarted.snapshots()[0].path, "dirty.py")

    def test_prune_clean_after_full_revert_deletes_snapshot_store(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
            root = Path(td)
            path = root / "app.py"
            path.write_text("old\n", encoding="utf-8")
            store = SnapshotStore(state_td)
            tracker = ChangeTracker(root, store)
            tracker.capture_before("app.py")
            path.write_text("new\n", encoding="utf-8")

            self.assertEqual(tracker.collect()["changed_count"], 1)
            path.write_text("old\n", encoding="utf-8")
            tracker.prune_clean()

            self.assertFalse(tracker.has_snapshots)
            self.assertFalse(store.path_for(root).exists())

    def test_capture_after_only_rewrites_the_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
            root = Path(td)
            path = root / "app.py"
            path.write_text("old\n", encoding="utf-8")
            store = SnapshotStore(state_td)
            tracker = ChangeTracker(root, store)
            tracker.capture_before("app.py")
            body = store._baseline_path(root, "app.py")
            manifest_before = store.path_for(root).read_text(encoding="utf-8")
            body_before = body.read_text(encoding="utf-8")

            path.write_text("new\n", encoding="utf-8")
            tracker.capture_after("app.py")

            # Baseline body untouched; only the small manifest gained a hash.
            self.assertEqual(body.read_text(encoding="utf-8"), body_before)
            manifest_after = store.path_for(root).read_text(encoding="utf-8")
            self.assertIn("after_hash", manifest_after)
            self.assertNotEqual(manifest_after, manifest_before)

            restarted = ChangeTracker(root, store)
            self.assertEqual(
                restarted.collect()["files"][0]["status"],
                "M",
            )

    def test_concurrent_collect_during_capture_never_loses_a_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
            root = Path(td)
            (root / "app.py").write_text("old\n", encoding="utf-8")
            store = SnapshotStore(state_td)
            tracker = ChangeTracker(root, store)
            errors: list[BaseException] = []

            def poller() -> None:
                try:
                    for _ in range(50):
                        tracker.collect()
                except BaseException as exc:  # pragma: no cover - surfaced below
                    errors.append(exc)

            thread = threading.Thread(target=poller)
            thread.start()
            try:
                for index in range(20):
                    tracker.capture_before(f"m{index}.py")
                    (root / f"m{index}.py").write_text(f"new {index}\n", encoding="utf-8")
                    tracker.capture_after(f"m{index}.py")
            finally:
                thread.join()

            self.assertEqual(errors, [])
            self.assertEqual(len(tracker._before), 20)

    def test_concurrent_capture_before_counts_total_bytes_once(self) -> None:
        # Threads racing capture_before on the same file used to each pay the
        # byte cost: one dict entry, _total_bytes incremented twice.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            content = "baseline\n"
            (root / "app.py").write_text(content, encoding="utf-8")
            tracker = ChangeTracker(root)

            start = threading.Barrier(4)

            def capture() -> None:
                start.wait()
                tracker.capture_before("app.py")

            threads = [threading.Thread(target=capture) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(list(tracker._before), ["app.py"])
            self.assertEqual(tracker._total_bytes, len(content.encode("utf-8")))

    def test_capture_after_pruned_during_hash_writes_no_orphan(self) -> None:
        # prune_clean dropping the baseline while capture_after is still
        # hashing must not leave an orphan after-hash behind.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("same\n", encoding="utf-8")
            tracker = ChangeTracker(root)
            tracker.capture_before("app.py")

            hashing = threading.Event()
            release = threading.Event()
            real_hash = changes._path_hash

            def slow_hash(path: Path) -> str:
                hashing.set()
                release.wait(2.0)
                return real_hash(path)

            with mock.patch.object(changes, "_path_hash", slow_hash):
                worker = threading.Thread(
                    target=lambda: tracker.capture_after("app.py")
                )
                worker.start()
                self.assertTrue(hashing.wait(2.0))
                self.assertEqual(tracker.prune_clean(), ["app.py"])
                release.set()
                worker.join()

            self.assertEqual(tracker._after_hashes, {})
            self.assertFalse(tracker.has_snapshots)

    def test_incompatible_manifest_layouts_are_ignored(self) -> None:
        # The recovery store reads exactly one manifest shape: an unknown
        # schema version or a legacy flat layout starts empty instead of
        # being half-interpreted.
        for payload in (
            '{"schema_version":99,"files":{}}',
            '{"schema_version":1,"before":{"../outside.py":"x"},"after_hashes":{}}',
        ):
            with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
                root = Path(td)
                store = SnapshotStore(state_td)
                path = store.path_for(root)
                path.parent.mkdir(parents=True)
                path.write_text(payload, encoding="utf-8")

                tracker = ChangeTracker(root, store)

                self.assertFalse(tracker.has_snapshots)

    def test_manifest_entries_require_canonical_baseline_shape(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
            root = Path(td)
            store = SnapshotStore(state_td)
            path = root / "app.py"
            path.write_text("old\n", encoding="utf-8")

            tracker = ChangeTracker(root, store)
            tracker.capture_before("app.py")
            manifest = store.path_for(root)
            original = manifest.read_text(encoding="utf-8")

            manifest.write_text(
                original.replace('"baseline":', '"extra":true,"baseline":'),
                encoding="utf-8",
            )
            with_extra_key = ChangeTracker(root, store)
            self.assertFalse(with_extra_key.has_snapshots)

            manifest.write_text(
                '{"schema_version":1,"files":{"app.py":{"after_hash":"missing"}}}',
                encoding="utf-8",
            )
            missing_baseline_key = ChangeTracker(root, store)
            self.assertFalse(missing_baseline_key.has_snapshots)

            manifest.write_text(
                original.replace(
                    store._baseline_path(root, "app.py").name,
                    "wrong-baseline.txt",
                ),
                encoding="utf-8",
            )
            wrong_baseline_ref = ChangeTracker(root, store)
            self.assertFalse(wrong_baseline_ref.has_snapshots)

    def test_snapshot_survives_abrupt_process_exit(self) -> None:
        script = (
            "import os,sys; from pathlib import Path; "
            "from codey.workspace.changes import ChangeTracker,SnapshotStore; "
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

    def test_concurrent_put_baseline_does_not_lose_entries(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
            root = Path(td)
            store = SnapshotStore(state_td)
            num_files = 20

            def writer(idx: int) -> None:
                rel = f"file_{idx}.txt"
                store.put_baseline(root, rel, f"content_{idx}")

            threads = [threading.Thread(target=writer, args=(i,)) for i in range(num_files)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            before, _ = store.load(root)
            self.assertEqual(len(before), num_files)
            for i in range(num_files):
                self.assertEqual(before.get(f"file_{i}.txt"), f"content_{i}")

    def test_snapshot_lock_file_lives_outside_snapshot_directory(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
            root = Path(td)
            store = SnapshotStore(state_td)

            store.put_baseline(root, "app.py", "old\n")

            self.assertTrue((store.dir_for(root).parent / ".recovery.lock").is_file())
            self.assertFalse((store.dir_for(root) / ".recovery.lock").exists())

    def test_missing_manifest_load_does_not_create_lock_directory(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
            root = Path(td)
            store = SnapshotStore(state_td)

            before, hashes = store.load(root)

            self.assertEqual(before, {})
            self.assertEqual(hashes, {})
            self.assertFalse(store.dir_for(root).parent.exists())

            store.remove(root, "app.py")
            store.delete(root)

            self.assertFalse(store.dir_for(root).parent.exists())

    def test_capture_before_failure_cleans_up_orphaned_body(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
            root = Path(td)
            store = SnapshotStore(state_td)
            tracker = ChangeTracker(root, store)
            file_path = root / "temp.txt"
            file_path.write_text("initial", encoding="utf-8")

            # Patch _update_manifest_locked to raise error during manifest write
            with mock.patch.object(store, "_update_manifest_locked", side_effect=OSError("disk full")), self.assertRaises(OSError):
                tracker.capture_before("temp.txt")

            # Verify memory state rolled back
            self.assertFalse(tracker.has_snapshots)
            # Verify body file cleaned up
            baseline_file = store._baseline_path(root, "temp.txt")
            self.assertFalse(baseline_file.exists())

    def test_status_nonzero_exit_is_explicit_error_not_empty_changes(self) -> None:
        from types import SimpleNamespace

        def _proc(args: list[str], stdout: str = "", returncode: int = 0) -> SimpleNamespace:
            return SimpleNamespace(
                args=args, returncode=returncode, stdout=stdout, stderr="",
                stdout_truncated=False, stderr_truncated=False,
            )

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            calls: list[list[str]] = []

            def run_git(_cwd: Path, args: list[str]) -> SimpleNamespace:
                calls.append(args)
                if args == ["rev-parse", "--show-toplevel"]:
                    return _proc(args, str(root))
                if args == ["status", "--short"]:
                    return _proc(args, "", returncode=128)
                raise AssertionError(f"unexpected git command after failure: {args}")

            with mock.patch.object(changes, "_run_git", side_effect=run_git):
                data = changes.collect_git_changes(root)

        self.assertFalse(data["ok"])
        self.assertIn("128", data["error"])
        self.assertIn("status", data["error"])
        self.assertEqual(
            calls,
            [["rev-parse", "--show-toplevel"], ["status", "--short"]],
        )

    def test_numstat_nonzero_exit_stops_before_diff(self) -> None:
        from types import SimpleNamespace

        def _proc(args: list[str], stdout: str = "", returncode: int = 0) -> SimpleNamespace:
            return SimpleNamespace(
                args=args, returncode=returncode, stdout=stdout, stderr="",
                stdout_truncated=False, stderr_truncated=False,
            )

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            calls: list[list[str]] = []

            def run_git(_cwd: Path, args: list[str]) -> SimpleNamespace:
                calls.append(args)
                if args == ["rev-parse", "--show-toplevel"]:
                    return _proc(args, str(root))
                if args == ["status", "--short"]:
                    return _proc(args, " M a.py\n")
                if args == ["diff", "--numstat"]:
                    return _proc(args, "", returncode=1)
                raise AssertionError(f"unexpected git command after failure: {args}")

            with mock.patch.object(changes, "_run_git", side_effect=run_git):
                data = changes.collect_git_changes(root)

        self.assertFalse(data["ok"])
        self.assertIn("numstat", data["error"])
        self.assertEqual(
            calls,
            [["rev-parse", "--show-toplevel"], ["status", "--short"], ["diff", "--numstat"]],
        )

    def test_diff_nonzero_exit_is_explicit_error(self) -> None:
        from types import SimpleNamespace

        def _proc(args: list[str], stdout: str = "", returncode: int = 0) -> SimpleNamespace:
            return SimpleNamespace(
                args=args, returncode=returncode, stdout=stdout, stderr="",
                stdout_truncated=False, stderr_truncated=False,
            )

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            calls: list[list[str]] = []

            def run_git(_cwd: Path, args: list[str]) -> SimpleNamespace:
                calls.append(args)
                if args == ["rev-parse", "--show-toplevel"]:
                    return _proc(args, str(root))
                if args == ["status", "--short"]:
                    return _proc(args, " M a.py\n")
                if args == ["diff", "--numstat"]:
                    return _proc(args, "1\t0\ta.py\n")
                if args == ["diff", "--cached", "--numstat"]:
                    return _proc(args, "")
                if args == ["diff", "--no-ext-diff", "--"]:
                    return _proc(args, "", returncode=1)
                raise AssertionError(f"unexpected git command after failure: {args}")

            with mock.patch.object(changes, "_run_git", side_effect=run_git):
                data = changes.collect_git_changes(root)

        self.assertFalse(data["ok"])
        self.assertIn("diff", data["error"])
        self.assertEqual(
            calls,
            [
                ["rev-parse", "--show-toplevel"],
                ["status", "--short"],
                ["diff", "--numstat"],
                ["diff", "--cached", "--numstat"],
                ["diff", "--no-ext-diff", "--"],
            ],
        )

    def test_revparse_nonzero_without_not_repo_stderr_is_git_failure(self) -> None:
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("old\n", encoding="utf-8")
            tracker = ChangeTracker(root)
            tracker.capture_before("app.py")
            (root / "app.py").write_text("new\n", encoding="utf-8")

            def run_git(_cwd: Path, args: list[str]) -> SimpleNamespace:
                assert args == ["rev-parse", "--show-toplevel"]
                return SimpleNamespace(
                    args=args, returncode=128, stdout="", stderr="fatal: unable to read tree (corrupt)",
                    stdout_truncated=False, stderr_truncated=False,
                )

            with mock.patch.object(changes, "_run_git", side_effect=run_git):
                failed = changes.collect_changes(root, tracker)
                ambiguous = changes.collect_git_changes(root)

        self.assertFalse(ambiguous["ok"])
        self.assertEqual(ambiguous["reason"], changes.GIT_REASON_FAILED)
        self.assertIn("128", ambiguous["error"])
        self.assertIn("unable to read tree", ambiguous["error"])
        # A tracker must not turn an ambiguous rev-parse failure into an
        # empty snapshot.
        self.assertFalse(failed["ok"])
        self.assertEqual(failed["reason"], changes.GIT_REASON_FAILED)
        self.assertTrue(tracker.has_snapshots)

    def test_revparse_not_repo_stderr_still_falls_back_to_tracker(self) -> None:
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("old\n", encoding="utf-8")
            tracker = ChangeTracker(root)
            tracker.capture_before("app.py")
            (root / "app.py").write_text("new\n", encoding="utf-8")

            def run_git(_cwd: Path, args: list[str]) -> SimpleNamespace:
                assert args == ["rev-parse", "--show-toplevel"]
                return SimpleNamespace(
                    args=args, returncode=128, stdout="",
                    stderr="fatal: not a git repository (or any of the parent directories): .git",
                    stdout_truncated=False, stderr_truncated=False,
                )

            with mock.patch.object(changes, "_run_git", side_effect=run_git):
                data = changes.collect_changes(root, tracker)

        self.assertTrue(data["ok"])
        self.assertEqual(data["mode"], "snapshot")
        self.assertEqual(data["changed_count"], 1)

    def test_collect_changes_reports_git_timeout_despite_tracker(self) -> None:
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("old\n", encoding="utf-8")
            tracker = ChangeTracker(root)
            tracker.capture_before("app.py")
            (root / "app.py").write_text("new\n", encoding="utf-8")

            def run_git(_cwd: Path, args: list[str]) -> SimpleNamespace:
                if args == ["rev-parse", "--show-toplevel"]:
                    return SimpleNamespace(
                        args=args, returncode=0, stdout=str(root), stderr="",
                        stdout_truncated=False, stderr_truncated=False,
                    )
                raise subprocess.TimeoutExpired(["git", *args], changes.GIT_TIMEOUT)

            with mock.patch.object(changes, "_run_git", side_effect=run_git):
                data = changes.collect_changes(root, tracker)

        self.assertFalse(data["ok"])
        self.assertIn("git command failed", data["error"])
        # The failure must not consume the tracker's recovery baseline.
        self.assertTrue(tracker.has_snapshots)

    def test_snapshots_collect_prune_interleave_has_no_keyerror(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "a.txt").write_text("old\n", encoding="utf-8")
            tracker = ChangeTracker(root)
            tracker.capture_before("a.txt")
            (root / "a.txt").write_text("new\n", encoding="utf-8")

            reading = threading.Event()
            proceed = threading.Event()
            real_read = changes._read_text_or_none
            gated = {"done": False}

            def slow_read(path: Path, *, max_bytes: int) -> str | None:
                if not gated["done"]:
                    gated["done"] = True
                    reading.set()
                    assert proceed.wait(10.0)
                return real_read(path, max_bytes=max_bytes)

            result: dict[str, object] = {}

            def collect() -> None:
                try:
                    result["snapshots"] = tracker.snapshots()
                except BaseException as exc:  # pragma: no cover - must not happen
                    result["error"] = exc

            with mock.patch.object(changes, "_read_text_or_none", side_effect=slow_read):
                worker = threading.Thread(target=collect)
                worker.start()
                self.assertTrue(reading.wait(10.0))
                # Revert so prune_clean drops the baseline while the collect
                # is still reading: the old lookup then raised KeyError.
                (root / "a.txt").write_text("old\n", encoding="utf-8")
                self.assertEqual(tracker.prune_clean(), ["a.txt"])
                proceed.set()
                worker.join(10.0)
                self.assertFalse(worker.is_alive())

            self.assertNotIn("error", result)
            # Start view said old, end state is old: no change to report.
            self.assertEqual(result["snapshots"], [])

    def test_single_dirty_entry_does_not_wipe_whole_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
            root = Path(td)
            store = SnapshotStore(state_td)
            (root / "good.py").write_text("good\n", encoding="utf-8")
            (root / "bad.py").write_text("bad\n", encoding="utf-8")

            tracker = ChangeTracker(root, store)
            tracker.capture_before("good.py")
            tracker.capture_before("bad.py")

            manifest = store.path_for(root)
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload["files"]["bad.py"]["after_hash"] = "not-a-hash"
            manifest.write_text(json.dumps(payload), encoding="utf-8")

            before, _ = SnapshotStore(state_td).load(root)

            self.assertIn("good.py", before)
            self.assertNotIn("bad.py", before)
    def test_corrupt_manifest_is_backed_up_not_silently_emptied(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
            root = Path(td)
            store = SnapshotStore(state_td)
            manifest = store.path_for(root)
            manifest.parent.mkdir(parents=True)
            manifest.write_text("not json", encoding="utf-8")

            before, hashes = SnapshotStore(state_td).load(root)

            self.assertEqual(before, {})
            self.assertEqual(hashes, {})
            self.assertFalse(manifest.exists())
            self.assertTrue(manifest.with_name(manifest.name + ".corrupt").exists())

    def test_dirty_entry_does_not_consume_total_budget(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
            root = Path(td)
            store = SnapshotStore(state_td)
            (root / "big.py").write_text("x" * 2000, encoding="utf-8")
            (root / "good.py").write_text("y\n", encoding="utf-8")
            tracker = ChangeTracker(root, store)
            tracker.capture_before("big.py")
            tracker.capture_before("good.py")

            manifest = store.path_for(root)
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload["files"]["big.py"]["after_hash"] = "not-a-hash"
            manifest.write_text(json.dumps(payload), encoding="utf-8")

            with mock.patch.object(changes, "MAX_SNAPSHOT_TOTAL_BYTES", 100):
                before, _ = SnapshotStore(state_td).load(root)

            self.assertNotIn("big.py", before)
            self.assertIn("good.py", before)


if __name__ == "__main__":
    unittest.main()
