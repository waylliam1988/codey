from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools import kobold_live_gate as gate


class KoboldLiveGateFixtureTests(unittest.TestCase):
    def test_all_agent_cases_have_fixtures(self) -> None:
        for case in ("create", "edit", "references", "discussion", "planning", "auto"):
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                gate._make_fixture(root, case)  # must not raise

    def test_empty_cases_create_no_files(self) -> None:
        for case in ("create", "discussion", "planning", "auto"):
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                gate._make_fixture(root, case)
                self.assertEqual(list(root.iterdir()), [])

    def test_edit_fixture_is_discoverable(self) -> None:
        from codey.completion.verification_policy import discover_verification_candidates

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            gate._make_fixture(root, "edit")
            candidates = discover_verification_candidates(str(root))
            commands = [item.command for item in candidates]
            self.assertIn("python -m unittest discover", commands)

    def test_discussion_and_planning_verify_rejects_files(self) -> None:
        for case in ("discussion", "planning"):
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                self.assertTrue(gate._verify_fixture(root, case)["ok"])
                (root / "unexpected.txt").write_text("changed", encoding="utf-8")
                self.assertFalse(gate._verify_fixture(root, case)["ok"])

    def test_auto_verify_requires_exact_content(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "hello_auto.txt"
            target.write_text("hello auto\n", encoding="utf-8")
            self.assertTrue(gate._verify_fixture(root, "auto")["ok"])
            target.write_text("hello auto\nthis should not be here\n", encoding="utf-8")
            self.assertFalse(gate._verify_fixture(root, "auto")["ok"])
            target.write_text("say hello auto please", encoding="utf-8")
            self.assertFalse(gate._verify_fixture(root, "auto")["ok"])

    def test_zero_discovered_tests_fail_verification(self) -> None:
        self.assertTrue(gate._ran_zero_tests("Ran 0 tests in 0.001s\nOK"))
        self.assertFalse(gate._ran_zero_tests("Ran 3 tests in 0.001s\nOK"))
        self.assertFalse(gate._ran_zero_tests(""))


if __name__ == "__main__":
    unittest.main()
