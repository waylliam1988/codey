"""Pytest home isolation must be a full temp path, visible to children."""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path


class PytestHomeIsolationTests(unittest.TestCase):
    def test_home_points_inside_temp_state_home(self) -> None:
        state_home = os.environ.get("PYTEST_STATE_HOME", "")
        self.assertTrue(state_home, "PYTEST_STATE_HOME must be set")
        home = os.environ.get("HOME", "")
        self.assertEqual(Path(home).resolve(), Path(state_home).resolve())

    def test_windows_home_vars_are_full_paths(self) -> None:
        if os.name != "nt":
            self.skipTest("windows only")
        root = Path(os.environ["PYTEST_STATE_HOME"]).resolve()
        self.assertEqual(Path(os.environ["USERPROFILE"]).resolve(), root)
        drive = str(root.drive)
        anchor_rel = str(root.anchor[len(drive):]) if drive else "\\"
        # HOMEDRIVE must be the drive, HOMEPATH the full remainder, not just root.
        self.assertEqual(os.environ.get("HOMEDRIVE"), drive)
        expected_homepath = str(root)[len(drive):] or anchor_rel
        self.assertEqual(os.environ.get("HOMEPATH"), expected_homepath)
        self.assertNotEqual(os.environ.get("HOMEPATH"), "\\", "HOMEPATH must not be bare root")

    def test_child_process_sees_isolated_home(self) -> None:
        code = "import os,sys;print(os.environ.get('HOME',''));print(os.environ.get('USERPROFILE','') if os.name=='nt' else 'n/a')"
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0)
        lines = proc.stdout.splitlines()
        self.assertTrue(lines)
        state_home = str(Path(os.environ["PYTEST_STATE_HOME"]).resolve())
        # At least HOME must resolve inside the isolated root.
        self.assertIn(state_home, [str(Path(p).resolve()) if p and p != "n/a" else p for p in lines])


if __name__ == "__main__":
    unittest.main()
