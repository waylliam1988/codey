"""Split-lock tests for build_project_map extraction + deterministic bug."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codey.workspace import map as project_map


class ProjectMapSplitTests(unittest.TestCase):
    def test_none_task_matches_empty_task(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "src").mkdir()
            (root / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")

            expected = project_map.build_project_map(root, task="")
            actual = project_map.build_project_map(root, task=None)  # type: ignore[arg-type]

            self.assertEqual(actual, expected)
            self.assertEqual(actual.focused_subtree, "")
            self.assertEqual(actual.symbol_overview, "")


if __name__ == "__main__":
    unittest.main()
