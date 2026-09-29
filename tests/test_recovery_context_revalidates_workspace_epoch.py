"""Unsafe replay must detect a workspace bump between slots in one batch."""
from __future__ import annotations

import unittest


class RecoveryContextRevalidatesWorkspaceEpochTests(unittest.TestCase):
    def test_final_epoch_check_rejects_workspace_bump_between_replays(self) -> None:
        from codey.operations.kernel_recovery import RecoveryContext, _verified_persisted_identity
        from codey.workspace.revision import WorkspaceIdentity

        first = WorkspaceIdentity.trusted_pair(2, "sha256:" + "ab" * 32)
        second = WorkspaceIdentity.trusted_pair(3, "sha256:" + "cd" * 32)

        class Store:
            def __init__(self):
                self.calls = 0

            def current_state(self, _project, *, ignored_paths=()):
                self.calls += 1
                return first if self.calls == 1 else second

        store = Store()
        ctx = RecoveryContext(project_path="project", revision_store=store)
        record = {
            "workspace_revision": first.revision,
            "workspace_fingerprint": first.fingerprint,
        }
        self.assertIsNotNone(
            _verified_persisted_identity(record, project_path="project", revision_store=store, recovery_ctx=ctx)
        )
        self.assertFalse(ctx.workspace_epoch_stable())


if __name__ == "__main__":
    unittest.main()
