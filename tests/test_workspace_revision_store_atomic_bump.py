"""WorkspaceRevisionStore.bump_state() scans fingerprint inside the lock.

Repro (hygiene): revision was written under with_file_lock, then the
fingerprint scan ran after the lock was released. Two concurrent tasks could
pair revision N with another moment's fingerprint.

Lock: workspace_fingerprint() is called while the revision file lock is
held, so one bump returns one atomic (revision, fingerprint) pair.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path


class WorkspaceRevisionStoreAtomicBumpTests(unittest.TestCase):
    def test_bump_scans_fingerprint_inside_file_lock(self) -> None:
        from codey.workspace import revision as rev_mod

        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as home:
            project = Path(td)
            (project / "a.py").write_text("x=1\n", encoding="utf-8")
            store = rev_mod.WorkspaceRevisionStore(Path(home))

            events: list[str] = []
            real_lock = rev_mod.with_file_lock
            real_fp = rev_mod.workspace_fingerprint

            def tracking_fp(proj, *, ignored_paths=()):
                events.append("fingerprint")
                return real_fp(proj, ignored_paths=ignored_paths)

            class _LockCtx:
                def __init__(self, inner):
                    self._inner = inner

                def __enter__(self):
                    events.append("lock-enter")
                    return self._inner.__enter__()

                def __exit__(self, *exc):
                    try:
                        return self._inner.__exit__(*exc)
                    finally:
                        events.append("lock-exit")

            def tracking_lock(path):
                return _LockCtx(real_lock(path))

            import unittest.mock as mock

            with mock.patch.object(rev_mod, "with_file_lock", side_effect=tracking_lock), mock.patch.object(
                rev_mod, "workspace_fingerprint", side_effect=tracking_fp
            ):
                # Rebind: bump_state looks up with_file_lock/workspace_fingerprint
                # from module globals at call time.
                state = store.bump_state(str(project), ignored_paths=())
            self.assertTrue(state.revision >= 1)
            self.assertTrue(state.fingerprint.startswith("sha256:"))
            self.assertIn("fingerprint", events, f"fingerprint must be scanned: {events}")
            self.assertIn("lock-enter", events)
            # Fingerprint must happen between lock-enter and lock-exit.
            enter = events.index("lock-enter")
            exit_ = events.index("lock-exit")
            fp_at = events.index("fingerprint")
            self.assertTrue(
                enter < fp_at < exit_,
                f"fingerprint must be inside the revision lock: {events}",
            )


if __name__ == "__main__":
    unittest.main()
