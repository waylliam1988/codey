"""Workspace state sync failures must not return a trusted pair."""
from __future__ import annotations

import unittest


class WorkspaceStateSyncFailureIsUntrustedTests(unittest.TestCase):
    def test_session_sync_failure_raises_recovery_failed(self) -> None:
        import tempfile
        from pathlib import Path

        from codey.operations.kernel_errors import RecoveryFailed
        from codey.operations.kernel_provenance import sync_workspace_state_after_edit
        from codey.workspace.revision import WorkspaceRevisionStore

        class BrokenSession:
            def set_workspace_state(self, _revision, _fingerprint):
                raise RuntimeError("session state unavailable")

        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as home:
            project = Path(td)
            (project / "a.py").write_text("x=1\n", encoding="utf-8")
            with self.assertRaises(RecoveryFailed):
                sync_workspace_state_after_edit(
                    BrokenSession(),
                    project,
                    revision_store=WorkspaceRevisionStore(home),
                )


if __name__ == "__main__":
    unittest.main()
