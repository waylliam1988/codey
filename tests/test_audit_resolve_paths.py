"""Audit entry points must survive a symlinked/short-name project root.

Root cause (found via CI-only red in test_consensus_audit_split.py):
``safe_join`` returns a *resolved* path while the audit ``allow_*`` guards
and ``relative_to`` calls use the *unresolved* caller root. When the root
itself contains a symlink (or a Windows 8.3 short-name component, as on CI
runners), every file/dir is silently rejected and search reports
"(no literal matches)" with ok=True.

These tests pin the contract with a symlinked root; they fail before the
fix and pass after. Skipped where the OS refuses symlink creation.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codey.agents import consensus


def _try_dir_symlink(link: Path, target: Path) -> bool:
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        return False
    return True


def _make_real_root(base: Path) -> Path:
    real = base / "real"
    real.mkdir(parents=True, exist_ok=True)
    (real / "a.py").write_text("marker one\n", encoding="utf-8")
    return real


class AuditSymlinkedRootTests(unittest.TestCase):
    def test_search_files_through_symlinked_root_finds_matches(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            real = _make_real_root(Path(td))
            link = Path(td) / "linkroot"
            if not _try_dir_symlink(link, real):
                self.skipTest("symlink creation unavailable")
            out = consensus._audit_search_files(link, ".", "marker")
            self.assertIn("a.py:1: marker one", out.model_text)

    def test_visible_entries_through_symlinked_root_lists_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            real = _make_real_root(Path(td))
            link = Path(td) / "linkroot"
            if not _try_dir_symlink(link, real):
                self.skipTest("symlink creation unavailable")
            out = consensus._audit_visible_entries(link, ".")
            self.assertIn("a.py", out.model_text)

    def test_read_file_through_symlinked_root_reads(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            real = _make_real_root(Path(td))
            link = Path(td) / "linkroot"
            if not _try_dir_symlink(link, real):
                self.skipTest("symlink creation unavailable")
            out = consensus._audit_read_file(link, "a.py")
            self.assertTrue(out.ok)
            self.assertIn("marker one", out.model_text)


if __name__ == "__main__":
    unittest.main()
