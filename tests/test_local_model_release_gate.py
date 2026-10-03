from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import local_model_release_gate as gate


class LocalModelReleaseGateFixtureTests(unittest.TestCase):
    def test_configured_endpoint_overrides_default_candidates(self) -> None:
        with mock.patch.dict("os.environ", {"LOCAL_OPENAI_BASE_URL": "http://model.test/v1"}, clear=False):
            self.assertEqual(gate.candidate_base_urls(), ("http://model.test/v1",))

    def test_cases_include_shared_hybrid_entry(self) -> None:
        self.assertIn("hybrid", gate.CASES)
        _task, intent, _turns = gate._task_for("hybrid")
        self.assertEqual(intent, "hybrid")

    def test_cases_include_tests_research_and_recovery_axes(self) -> None:
        for case in ("tests", "research", "recovery"):
            self.assertIn(case, gate.CASES)
            if case != "recovery":
                gate._task_for(case)

    def test_default_cases_include_configured_research(self) -> None:
        self.assertIn("research", gate.DEFAULT_CASES)
        self.assertIn("research", gate.CASES)

    def test_all_agent_cases_have_fixtures(self) -> None:
        for case in (
            "create", "edit", "references", "hybrid", "discussion", "planning",
            "auto", "tests", "research",
        ):
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                gate._make_fixture(root, case)  # must not raise

    def test_empty_cases_create_no_files(self) -> None:
        for case in ("create", "discussion", "auto", "research"):
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

    def test_hybrid_fixture_is_discoverable(self) -> None:
        from codey.completion.verification_policy import discover_verification_candidates

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            gate._make_fixture(root, "hybrid")
            candidates = discover_verification_candidates(str(root))
            self.assertIn("python -m unittest discover", [item.command for item in candidates])

    def test_tests_fixture_starts_without_tests(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            gate._make_fixture(root, "tests")
            self.assertTrue((root / "calculator.py").is_file())
            self.assertFalse((root / "tests" / "test_calculator.py").exists())

    def test_research_order_requires_open_source_before_done(self) -> None:
        rows = [
            {"type": "tool", "tool_name": "web_search", "ok": True, "run_id": "r", "session_id": "s"},
            {"type": "task_done", "stop_reason": "done", "run_id": "r", "session_id": "s"},
        ]
        self.assertFalse(gate.check_research_tool_order(rows)["ok"])
        rows.insert(1, {"type": "tool", "tool_name": "open_url", "ok": True, "run_id": "r", "session_id": "s"})
        self.assertTrue(gate.check_research_tool_order(rows)["ok"])

    def test_discussion_and_planning_verify_rejects_files(self) -> None:
        for case in ("discussion", "planning"):
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                gate._make_fixture(root, case)
                baseline = gate.fixture_file_hashes(root)
                self.assertTrue(gate._verify_fixture(root, case, baseline_files=baseline)["ok"])
                (root / "unexpected.txt").write_text("changed", encoding="utf-8")
                self.assertFalse(gate._verify_fixture(root, case, baseline_files=baseline)["ok"])

    def test_planning_has_real_source_to_inspect(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            gate._make_fixture(root, "planning")
            self.assertIn("def calculate_total", (root / "pricing.py").read_text(encoding="utf-8"))

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

    def test_ghost_roundtrip_preserves_case_identity(self) -> None:
        result = gate.run_ghost_case()
        self.assertEqual(result["case"], "ghost")
        self.assertTrue(result["ok"])


if __name__ == "__main__":
    unittest.main()
