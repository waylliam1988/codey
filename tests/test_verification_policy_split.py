"""Refactor locks + deterministic bug hunts for verification_policy."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codey.completion.verification_policy import (
    VerificationCandidate,
    _command_priority,
    _make_targets,
    discover_verification_candidates,
)


class MakeTargetsRecipeTests(unittest.TestCase):
    def test_tab_indented_recipe_is_not_a_target(self) -> None:
        text = "lint:\n\tcheck: foo\n"
        self.assertEqual(_make_targets(text), ("lint",))


class PrioritySmokeTests(unittest.TestCase):
    def test_node_test_priority(self) -> None:
        candidate = VerificationCandidate("npm test", ".", "package.json script test")
        self.assertEqual(_command_priority(candidate), 95)

    def test_pytest_priority(self) -> None:
        candidate = VerificationCandidate("python -m pytest", ".", "pytest.ini")
        self.assertEqual(_command_priority(candidate), 100)

    def test_make_test_priority(self) -> None:
        candidate = VerificationCandidate("make test", ".", "Makefile target test")
        self.assertEqual(_command_priority(candidate), 50)


class DiscoverSmokeTests(unittest.TestCase):
    def test_discovers_pytest_ini(self) -> None:
        with tempfile.TemporaryDirectory() as td, mock.patch(
            "codey.completion.verification_policy.shutil.which", return_value="python"
        ):
            root = Path(td)
            (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
            candidates = discover_verification_candidates(root)

        self.assertIn(
            VerificationCandidate("python -m pytest", ".", "pytest.ini"), candidates
        )


if __name__ == "__main__":
    unittest.main()
