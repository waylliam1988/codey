from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codey.runtime import cancellation
from codey.reviews.impact_map import render_review_impact_map, safe_review_impact_map


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class ReviewImpactMapTests(unittest.TestCase):
    def test_finds_external_caller_and_test_without_source_body(self) -> None:
        changes = {
            "ok": True,
            "changed_count": 1,
            "files": [{"path": "src/format.ts", "status": "M"}],
            "diff": (
                "diff --git a/src/format.ts b/src/format.ts\n"
                "--- a/src/format.ts\n"
                "+++ b/src/format.ts\n"
                "@@ -1,3 +1,3 @@\n"
                "-export function formatTotal(cents: number): string {\n"
                "+export function formatCurrency(cents: number): string {\n"
                "   return `$${(cents / 100).toFixed(2)}`;\n"
                " }\n"
            ),
        }
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write(root, "src/format.ts", "export function formatCurrency() {}\n")
            _write(
                root,
                "src/view.ts",
                "import { formatTotal } from './format';\n"
                "export const label = formatTotal(1234);\n",
            )
            _write(
                root,
                "tests/format.test.ts",
                "import { formatTotal } from '../src/format';\n"
                "expect(formatTotal(1234)).toBe('$12.34');\n",
            )

            impact_map = render_review_impact_map(root, changes)

        self.assertIn("Review Impact Map (bounded hints; not coverage proof)", impact_map)
        self.assertIn("formatTotal -> formatCurrency", impact_map)
        self.assertIn("src/view.ts:", impact_map)
        self.assertIn("tests/format.test.ts:", impact_map)
        self.assertIn("findings[].path must still be a changed file", impact_map)
        self.assertNotIn("export const label", impact_map)
        self.assertNotIn("expect(formatTotal", impact_map)

    def test_returns_empty_without_changed_symbol(self) -> None:
        changes = {
            "ok": True,
            "changed_count": 1,
            "files": [{"path": "app.py", "status": "M"}],
            "diff": (
                "diff --git a/app.py b/app.py\n"
                "--- a/app.py\n"
                "+++ b/app.py\n"
                "@@ -1,1 +1,1 @@\n"
                "-value = 1\n"
                "+value = 2\n"
            ),
        }
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(render_review_impact_map(td, changes), "")

    def test_changed_file_matches_do_not_consume_external_reference_budget(self) -> None:
        changes = {
            "ok": True,
            "changed_count": 1,
            "files": [{"path": "src/api.py", "status": "M"}],
            "diff": (
                "diff --git a/src/api.py b/src/api.py\n"
                "--- a/src/api.py\n"
                "+++ b/src/api.py\n"
                "@@ -1,1 +1,1 @@\n"
                "-def old_name():\n"
                "+def new_name():\n"
            ),
        }
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write(
                root,
                "src/api.py",
                "def new_name():\n"
                "    return old_name()\n" * 40,
            )
            _write(
                root,
                "tests/test_api.py",
                "from src.api import old_name\n"
                "def test_api():\n"
                "    assert old_name() is not None\n",
            )

            impact_map = render_review_impact_map(
                root,
                changes,
                max_refs_per_symbol=1,
            )

        self.assertIn("tests/test_api.py:", impact_map)
        self.assertNotIn("Test reference hints:\n- (none found", impact_map)

    def test_changed_file_does_not_consume_scan_byte_budget(self) -> None:
        changes = {
            "ok": True,
            "changed_count": 1,
            "files": [{"path": "src/api.py", "status": "M"}],
            "diff": (
                "diff --git a/src/api.py b/src/api.py\n"
                "--- a/src/api.py\n"
                "+++ b/src/api.py\n"
                "@@ -1,1 +1,1 @@\n"
                "-def old_name():\n"
                "+def new_name():\n"
            ),
        }
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write(root, "src/api.py", "def new_name():\n" + ("old_name()\n" * 200))
            _write(root, "tests/test_api.py", "old_name()\n")

            with mock.patch("codey.reviews.impact_map.MAX_SCAN_BYTES", 80):
                impact_map = render_review_impact_map(root, changes)

        self.assertIn("tests/test_api.py:", impact_map)
        self.assertNotIn("omitted files may contain more references", impact_map)

    def test_test_reference_keeps_slot_when_callers_are_many(self) -> None:
        changes = {
            "ok": True,
            "changed_count": 1,
            "files": [{"path": "src/api.py", "status": "M"}],
            "diff": (
                "diff --git a/src/api.py b/src/api.py\n"
                "--- a/src/api.py\n"
                "+++ b/src/api.py\n"
                "@@ -1,1 +1,1 @@\n"
                "-def old_name():\n"
                "+def new_name():\n"
            ),
        }
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write(root, "src/api.py", "def new_name():\n    return None\n")
            for index in range(5):
                _write(root, f"src/caller{index}.py", "result = old_name()\n")
            _write(root, "tests/test_api.py", "assert old_name() is not None\n")

            impact_map = render_review_impact_map(
                root,
                changes,
                max_refs_per_symbol=4,
            )

        self.assertIn("tests/test_api.py:", impact_map)
        self.assertNotIn("Test reference hints:\n- (none found", impact_map)
        self.assertEqual(impact_map.count("src/caller"), 3)

    def test_global_reference_cap_preserves_later_symbol_test_refs(self) -> None:
        changes = {
            "ok": True,
            "changed_count": 1,
            "files": [{"path": "src/api.py", "status": "M"}],
            "diff": (
                "diff --git a/src/api.py b/src/api.py\n"
                "--- a/src/api.py\n"
                "+++ b/src/api.py\n"
                "@@ -1,6 +1,6 @@\n"
                "-def old_alpha():\n"
                "+def new_alpha():\n"
                "     return None\n"
                "-def old_beta():\n"
                "+def new_beta():\n"
                "     return None\n"
                "-def old_gamma():\n"
                "+def new_gamma():\n"
                "     return None\n"
            ),
        }
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write(root, "src/api.py", "def new_alpha(): pass\n")
            for symbol in ("alpha", "beta", "gamma"):
                for index in range(3):
                    _write(
                        root,
                        f"src/{symbol}_caller_{index}.py",
                        f"result = old_{symbol}()\n",
                    )
                _write(root, f"tests/test_{symbol}.py", f"assert old_{symbol}()\n")

            impact_map = render_review_impact_map(
                root,
                changes,
                max_refs_per_symbol=4,
                max_rendered_refs=10,
            )

        self.assertIn("tests/test_alpha.py:", impact_map)
        self.assertIn("tests/test_beta.py:", impact_map)
        self.assertIn("tests/test_gamma.py:", impact_map)
        self.assertIn("scan was bounded", impact_map)

    def test_global_reference_cap_fairly_keeps_test_and_caller_per_symbol(self) -> None:
        changes = {
            "ok": True,
            "changed_count": 1,
            "files": [{"path": "src/api.py", "status": "M"}],
            "diff": (
                "diff --git a/src/api.py b/src/api.py\n"
                "--- a/src/api.py\n"
                "+++ b/src/api.py\n"
                "@@ -1,4 +1,4 @@\n"
                "-def old_alpha():\n"
                "+def new_alpha():\n"
                "     return None\n"
                "-def old_beta():\n"
                "+def new_beta():\n"
                "     return None\n"
            ),
        }
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write(root, "src/api.py", "def new_alpha(): pass\n")
            for symbol in ("alpha", "beta"):
                for index in range(3):
                    _write(
                        root,
                        f"src/{symbol}_caller_{index}.py",
                        f"result = old_{symbol}()\n",
                    )
                _write(root, f"tests/test_{symbol}.py", f"assert old_{symbol}()\n")

            impact_map = render_review_impact_map(
                root,
                changes,
                max_refs_per_symbol=4,
                max_rendered_refs=4,
            )

        self.assertIn("src/alpha_caller_0.py:", impact_map)
        self.assertIn("src/beta_caller_0.py:", impact_map)
        self.assertIn("tests/test_alpha.py:", impact_map)
        self.assertIn("tests/test_beta.py:", impact_map)
        self.assertNotIn("src/alpha_caller_1.py:", impact_map)
        self.assertIn("scan was bounded", impact_map)

    def test_safe_wrapper_swallows_non_cancellation_errors(self) -> None:
        with mock.patch(
            "codey.reviews.impact_map.render_review_impact_map",
            side_effect=OSError("scan failed"),
        ):
            self.assertEqual(safe_review_impact_map("E:/missing", {}), "")

    def test_safe_wrapper_preserves_task_cancellation(self) -> None:
        with mock.patch(
            "codey.reviews.impact_map.render_review_impact_map",
            side_effect=cancellation.TaskCancelled("stop"),
        ):
            with self.assertRaises(cancellation.TaskCancelled):
                safe_review_impact_map("E:/demo", {})

    def test_render_budget_is_explicit(self) -> None:
        changes = {
            "ok": True,
            "changed_count": 1,
            "files": [{"path": "src/api.ts", "status": "M"}],
            "diff": (
                "diff --git a/src/api.ts b/src/api.ts\n"
                "--- a/src/api.ts\n"
                "+++ b/src/api.ts\n"
                "@@ -1,1 +1,1 @@\n"
                "-export function oldName() {}\n"
                "+export function newName() {}\n"
            ),
        }
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write(root, "src/api.ts", "export function newName() {}\n")
            _write(root, "src/a.ts", "oldName();\n")
            text = render_review_impact_map(root, changes, max_chars=120)

        self.assertIn("impact map truncated", text)


if __name__ == "__main__":
    unittest.main()