"""Missing revision file must not pair stale revision 1 with a new fingerprint.

Repro: ``current_state()`` returns revision 1 with a lock-outside fingerprint
scan when the revision file does not exist. If another worker performs the
first ``bump_state()`` during the scan, the caller pairs old revision 1 with
the new fingerprint.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock


class WorkspaceCurrentStateMissingFileConcurrentInitTests(unittest.TestCase):
    def test_missing_file_concurrent_first_bump_is_consistent(self) -> None:
        from codey.workspace import revision as rev_mod
        from codey.workspace.revision import WorkspaceRevisionStore

        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as home:
            project = Path(td)
            (project / "a.py").write_text("x=1\n", encoding="utf-8")
            store = WorkspaceRevisionStore(Path(home))
            real_fingerprint = rev_mod.workspace_fingerprint
            calls = {"n": 0}

            def racing_fingerprint(proj, *, ignored_paths=()):
                calls["n"] += 1
                if calls["n"] != 1:
                    return real_fingerprint(proj, ignored_paths=ignored_paths)
                fp_before = real_fingerprint(proj, ignored_paths=ignored_paths)
                # Concurrent worker performs the first durable bump while the
                # first caller is still scanning. Use the real fingerprint
                # for the bump's internal scan to avoid recursing into this
                # patched wrapper.
                with mock.patch.object(rev_mod, "workspace_fingerprint", real_fingerprint):
                    other = WorkspaceRevisionStore(Path(home))
                    bumped = other.bump_state(proj, ignored_paths=ignored_paths)
                fp_after = real_fingerprint(proj, ignored_paths=ignored_paths)
                # Return the post-bump fingerprint to simulate observing the
                # new file state; a stale init would pair revision 1 with it.
                self.assertTrue(bumped.revision >= 2)
                return fp_after or fp_before

            with mock.patch.object(rev_mod, "workspace_fingerprint", side_effect=racing_fingerprint):
                state = store.current_state(str(project), ignored_paths=())
            # Must not return stale revision 1 alongside the post-bump
            # fingerprint: either the pre-bump pair or the bumped pair, but
            # the revision on disk and the returned revision must agree.
            on_disk = store.current_state(str(project), ignored_paths=()).revision
            # After the race the durable revision is >= 2; returning 1 with
            # the new fingerprint is the bug.
            self.assertNotEqual(
                (state.revision, on_disk),
                (1, 2),
                f"stale init paired revision 1 with post-bump state: {state!r} disk={on_disk!r}",
            )
            self.assertGreaterEqual(state.revision, 2, f"must observe concurrent init: {state!r}")


if __name__ == "__main__":
    unittest.main()
