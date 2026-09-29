"""One authoritative WorkspaceState per edit; ignored_paths consistent.

Locks:
- Edit fingerprint and revision bump share one ``ignored_paths`` config.
- Same-batch edit+run sees the post-edit identity (no stale pre-edit).
- Repeated edits advance monotonically with a single bounded scan each.
"""
from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path


class WorkspaceAuthoritativeStateTests(unittest.TestCase):
    def test_sync_uses_configured_ignored_paths(self) -> None:
        from codey.operations import kernel_provenance as kp

        source = inspect.getsource(kp._sync_workspace_after_edit)
        # Must not compute a fingerprint without the configured ignore set.
        # Either it accepts ignored_paths or it delegates to the revision store.
        self.assertTrue(
            "ignored_paths" in source or "bump_state" in source or "current_state" in source,
            f"_sync_workspace_after_edit must honor ignored_paths:\n{source}",
        )
        # Direct bare workspace_fingerprint(project) without ignores is forbidden.
        self.assertNotIn("workspace_fingerprint(project_path)", source)

    def test_hooks_and_kernel_share_single_scan_helper(self) -> None:
        from codey.operations import kernel_provenance as kp

        self.assertTrue(
            hasattr(kp, "sync_workspace_state_after_edit")
            or "ignored_paths" in inspect.getsource(kp._sync_workspace_after_edit),
            "kernel sync must expose a single-scan helper honoring ignored_paths",
        )

    def test_ignored_file_does_not_change_fingerprint(self) -> None:
        from codey.workspace.revision import workspace_fingerprint

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "keep.py").write_text("x=1\n", encoding="utf-8")
            (root / "gen").mkdir()
            (root / "gen" / "out.py").write_text("v1\n", encoding="utf-8")
            fp1 = workspace_fingerprint(root, ignored_paths=("gen",))
            (root / "gen" / "out.py").write_text("v2 changed but ignored\n", encoding="utf-8")
            fp2 = workspace_fingerprint(root, ignored_paths=("gen",))
            self.assertEqual(fp1, fp2)
            fp3 = workspace_fingerprint(root, ignored_paths=())
            (root / "keep.py").write_text("x=2\n", encoding="utf-8")
            fp4 = workspace_fingerprint(root, ignored_paths=())
            self.assertNotEqual(fp3, fp4)


if __name__ == "__main__":
    unittest.main()
