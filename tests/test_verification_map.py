from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codey.verification_map import build_verification_map
from codey.work_checkpoint import CheckpointCheck


def _changes(*paths: str, diff: str = "") -> dict:
    return {
        "files": [{"path": path, "status": "M"} for path in paths],
        "diff": diff,
    }


class VerificationMapTests(unittest.TestCase):
    def test_python_naming_and_direct_import_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "src" / "auth").mkdir(parents=True)
            (root / "tests").mkdir()
            (root / "tests" / "test_token.py").write_text(
                "from auth.token import validate_token\n",
                encoding="utf-8",
            )
            (root / "tests" / "test_login.py").write_text(
                "import auth.token\n",
                encoding="utf-8",
            )

            result = build_verification_map(
                root,
                _changes("src/auth/token.py", diff="+def validate_token(value):\n+    return value"),
            )

        candidates = {item.path: item for item in result.test_candidates}
        self.assertEqual(candidates["tests/test_token.py"].evidence, "naming")
        self.assertEqual(candidates["tests/test_login.py"].evidence, "import")

    def test_python_deep_module_does_not_match_unrelated_bare_import(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "tests").mkdir()
            (root / "tests" / "test_unrelated.py").write_text(
                "import token\n",
                encoding="utf-8",
            )

            result = build_verification_map(
                root,
                _changes("src/auth/token.py"),
            )

        self.assertEqual(result.test_candidates, ())

    def test_javascript_test_and_spec_naming_and_relative_import(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "src").mkdir()
            (root / "tests").mkdir()
            (root / "src" / "router.test.ts").write_text("test('x', () => {})\n", encoding="utf-8")
            (root / "tests" / "routes.spec.ts").write_text(
                'import { createRouter } from "../src/router";\n',
                encoding="utf-8",
            )

            result = build_verification_map(
                root,
                _changes("src/router.ts", diff="+export function createRouter() {}"),
            )

        candidates = {item.path: item for item in result.test_candidates}
        self.assertEqual(candidates["src/router.test.ts"].evidence, "naming")
        self.assertEqual(candidates["tests/routes.spec.ts"].evidence, "import")

    def test_changed_test_and_changed_symbol_reference_are_distinguished(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "src").mkdir()
            (root / "tests").mkdir()
            (root / "tests" / "test_session.py").write_text(
                "def test_it():\n    assert SessionStore\n",
                encoding="utf-8",
            )

            result = build_verification_map(
                root,
                _changes(
                    "src/store.py",
                    "tests/test_changed.py",
                    diff="+class SessionStore:\n+    pass",
                ),
            )

        self.assertEqual(result.changed_tests, ("tests/test_changed.py",))
        candidate = next(item for item in result.test_candidates if item.path == "tests/test_session.py")
        self.assertEqual(candidate.evidence, "reference")

    def test_observed_checks_and_broader_commands_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            result = build_verification_map(
                td,
                _changes("app.py"),
                checks_after_last_change=(
                    CheckpointCheck("python -m pytest tests/test_app.py", "."),
                ),
                project_map=(
                    "Project Map:\n"
                    "Observed successful checks:\n"
                    "- successful check from web: npm test\n"
                    "Candidate commands (inspect before running):\n"
                    "- python -m pytest\n"
                    "- python -m ruff check .\n"
                ),
            )

        self.assertEqual(result.observed_checks[0].command, "python -m pytest tests/test_app.py")
        self.assertEqual(
            result.broader_commands,
            ("web/: npm test", "python -m pytest", "python -m ruff check ."),
        )

    def test_recommended_commands_replace_project_map_broader_commands(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            result = build_verification_map(
                td,
                _changes("app.py"),
                recommended_commands=("pnpm test",),
                project_map=(
                    "Project Map:\n"
                    "Candidate commands (inspect before running):\n"
                    "- npm test\n"
                ),
            )

        rendered = result.render()
        self.assertIn("Recommended local check candidates:", rendered)
        self.assertIn("- pnpm test", rendered)
        self.assertNotIn("- npm test", rendered)
        self.assertNotIn("Broader check candidates", rendered)

    def test_empty_result_is_honest(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            rendered = build_verification_map(td, _changes("app.py")).render()

        self.assertIn("(none found; this does not prove", rendered)
        self.assertIn("not coverage proof", rendered)

    def test_scan_skips_secret_symlink_binary_and_large_test_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tests = root / "tests"
            tests.mkdir()
            secret_dir = root / "secret-tests"
            secret_dir.mkdir()
            (secret_dir / "test_hidden.py").write_text("validate_token", encoding="utf-8")
            (tests / "test_secret.env").write_text("validate_token", encoding="utf-8")
            (tests / "test_binary.py").write_bytes(b"\xff\xfevalidate_token")
            (tests / "test_large.py").write_text("validate_token", encoding="utf-8")
            target = tests / "real_test.py"
            target.write_text("validate_token", encoding="utf-8")
            link = tests / "test_link.py"
            try:
                link.symlink_to(target)
            except OSError:
                pass

            with mock.patch("codey.verification_map.MAX_TEST_FILE_BYTES", 4):
                result = build_verification_map(
                    root,
                    _changes("src/token.py", diff="+def validate_token():\n+    pass"),
                )

        self.assertEqual(result.test_candidates, ())

    def test_candidate_limit_marks_only_real_overflow(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tests = root / "tests"
            tests.mkdir()
            for index in range(2):
                (tests / f"test_{index}.py").write_text("changed_symbol\n", encoding="utf-8")
            changes = _changes("src/app.py", diff="+def changed_symbol():\n+    pass")
            with mock.patch("codey.verification_map.MAX_CANDIDATES", 2):
                exact = build_verification_map(root, changes)
            (tests / "test_2.py").write_text("changed_symbol\n", encoding="utf-8")
            with mock.patch("codey.verification_map.MAX_CANDIDATES", 2):
                overflow = build_verification_map(root, changes)

        self.assertFalse(exact.truncated)
        self.assertTrue(overflow.truncated)
        self.assertEqual(len(overflow.test_candidates), 2)

    def test_truncated_diff_marks_map_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            changes = _changes("app.py")
            changes["truncated"] = True
            result = build_verification_map(td, changes)

        self.assertTrue(result.truncated)
        self.assertIn("additional relevant tests may exist", result.render())

    def test_changed_tests_only_do_not_scan_unrelated_tests(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "tests").mkdir()
            (root / "tests" / "test_other.py").write_text("changed_symbol", encoding="utf-8")
            with mock.patch("codey.verification_map.iter_bounded_files") as scan:
                result = build_verification_map(
                    root,
                    _changes("tests/test_changed.py", diff="+def changed_symbol():\n+    pass"),
                )

        scan.assert_not_called()
        self.assertEqual(result.changed_tests, ("tests/test_changed.py",))

    def test_scan_and_render_budgets_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "tests").mkdir()
            (root / "tests" / "test_app.py").write_text("changed_symbol", encoding="utf-8")
            changes = _changes("app.py", diff="+def changed_symbol():\n+    pass")
            with mock.patch("codey.verification_map.MAX_SCAN_DIRS", 0):
                limited = build_verification_map(root, changes)
            with mock.patch("codey.verification_map.MAX_RENDER_CHARS", 80):
                clipped = build_verification_map(root, changes).render()

        self.assertTrue(limited.truncated)
        self.assertIn("map truncated by character budget", clipped)

    def test_total_byte_budget_marks_only_overflow_and_keeps_prior_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tests = root / "tests"
            tests.mkdir()
            content = "changed_symbol\n"
            for index in range(2):
                (tests / f"test_{index}.py").write_text(content, encoding="utf-8")
            size = (tests / "test_0.py").stat().st_size
            changes = _changes("app.py", diff="+def changed_symbol():\n+    pass")
            with mock.patch("codey.verification_map.MAX_SCAN_TOTAL_BYTES", size * 2):
                exact = build_verification_map(root, changes)
            (tests / "test_2.py").write_text(content, encoding="utf-8")
            with mock.patch("codey.verification_map.MAX_SCAN_TOTAL_BYTES", size * 2):
                overflow = build_verification_map(root, changes)

        self.assertFalse(exact.truncated)
        self.assertEqual(len(exact.test_candidates), 2)
        self.assertTrue(overflow.truncated)
        self.assertEqual(len(overflow.test_candidates), 2)
        self.assertIn("additional relevant tests may exist", overflow.render())

    def test_non_utf8_read_attempt_consumes_total_byte_budget(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tests = root / "tests"
            tests.mkdir()
            candidate = tests / "test_0.py"
            candidate.write_text("changed_symbol\n", encoding="utf-8")
            bad = b"\xff\xfe\xff\xfe\xff\xfe\xff\xfe"
            (tests / "test_1.py").write_bytes(bad)
            (tests / "test_2.py").write_bytes(bad)
            budget = candidate.stat().st_size + len(bad)
            with mock.patch("codey.verification_map.MAX_SCAN_TOTAL_BYTES", budget):
                result = build_verification_map(
                    root,
                    _changes("app.py", diff="+def changed_symbol():\n+    pass"),
                )

        self.assertTrue(result.truncated)
        self.assertEqual(
            [item.path for item in result.test_candidates],
            ["tests/test_0.py"],
        )
        self.assertIn("additional relevant tests may exist", result.render())


if __name__ == "__main__":
    unittest.main()
