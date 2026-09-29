"""current_state version check retries on concurrent bump.

Repro: ``current_state()`` read revision N, scanned fingerprint outside the
lock, and returned (N, fp) even when a concurrent ``bump_state()`` changed
the revision mid-scan, pairing the old revision with the new fingerprint.

Lock: a revision change between the pre-scan and post-scan reads retries
once with the authoritative inside-lock scan; the returned pair is always
from the same moment.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock


class CurrentStateVersionCheckRetriesTests(unittest.TestCase):
    def test_concurrent_bump_during_scan_returns_consistent_pair(self) -> None:
        from codey.workspace.revision import WorkspaceRevisionStore

        with tempfile.TemporaryDirectory() as td:
            project = Path(td) / "project"
            project.mkdir()
            store = WorkspaceRevisionStore(Path(td) / "state")
            path = store.path_for(project)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"schema_version": 1, "revision": 1}', encoding="utf-8")

            outside_fp = "sha256:" + "a" * 64
            inside_fp = "sha256:" + "b" * 64
            reads = iter([1, 2, 2])

            def fake_read(_path: Path) -> int:
                return next(reads)

            fps = iter([outside_fp, inside_fp])

            def fake_fp(_project: object, *, ignored_paths: object = ()) -> str:
                return next(fps)

            with (
                mock.patch.object(
                    WorkspaceRevisionStore, "_read_revision_unlocked",
                    side_effect=fake_read,
                ),
                mock.patch(
                    "codey.workspace.revision.workspace_fingerprint", side_effect=fake_fp
                ),
            ):
                state = store.current_state(project)

            self.assertEqual(state.revision, 2)
            self.assertEqual(state.fingerprint, inside_fp)

    def test_stable_revision_uses_outside_scan(self) -> None:
        from codey.workspace.revision import WorkspaceRevisionStore

        with tempfile.TemporaryDirectory() as td:
            project = Path(td) / "project"
            project.mkdir()
            store = WorkspaceRevisionStore(Path(td) / "state")
            path = store.path_for(project)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"schema_version": 1, "revision": 5}', encoding="utf-8")

            fp = "sha256:" + "c" * 64
            with mock.patch(
                "codey.workspace.revision.workspace_fingerprint",
                return_value=fp,
            ):
                state = store.current_state(project)

            self.assertEqual(state.revision, 5)
            self.assertEqual(state.fingerprint, fp)


if __name__ == "__main__":
    unittest.main()
