from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from codey import project_map


class ProjectMapTests(unittest.TestCase):
    def test_directory_entry_scan_stops_after_remaining_budget_plus_probe(self) -> None:
        class FakeEntry:
            def __init__(self, name: str) -> None:
                self.name = name

            def is_dir(self) -> bool:
                return False

        class FakeDirectory:
            def __init__(self) -> None:
                self.seen = 0

            def iterdir(self):
                for index in range(100):
                    self.seen += 1
                    if self.seen > 4:
                        raise AssertionError("iterator consumed past remaining + 1")
                    yield FakeEntry(f"file_{index}.py")

        directory = FakeDirectory()

        entries, truncated = project_map._bounded_directory_entries(directory, 3)

        self.assertTrue(truncated)
        self.assertEqual(directory.seen, 4)
        self.assertEqual(len(entries), 3)

    def test_node_project_detects_manifest_scripts_and_roots(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "src").mkdir()
            (root / "src" / "app.ts").write_text("export const value = 1;\n", encoding="utf-8")
            (root / "tests").mkdir()
            (root / "tests" / "app.test.ts").write_text("test('ok', () => {})\n", encoding="utf-8")
            (root / "README.md").write_text("# demo\n", encoding="utf-8")
            (root / "package.json").write_text(
                json.dumps({
                    "scripts": {
                        "test": "vitest",
                        "lint": "eslint .",
                        "build": "vite build",
                    }
                }),
                encoding="utf-8",
            )

            rendered = project_map.render_project_map(root)

        self.assertIn("Project Map", rendered)
        self.assertIn("src/", rendered)
        self.assertIn("tests/", rendered)
        self.assertIn("package.json", rendered)
        self.assertIn("README.md", rendered)
        self.assertIn("npm test", rendered)
        self.assertIn("npm run lint", rendered)
        self.assertIn("npm run build", rendered)
        self.assertIn("Candidate commands (inspect before running)", rendered)

    def test_nested_manifest_candidate_commands_include_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "web").mkdir()
            (root / "web" / "package.json").write_text(
                json.dumps({"scripts": {"test": "vitest", "build": "vite build"}}),
                encoding="utf-8",
            )

            rendered = project_map.render_project_map(root)

        self.assertIn("web/package.json", rendered)
        self.assertIn("web/: npm test", rendered)
        self.assertIn("web/: npm run build", rendered)
        self.assertNotIn("\n- npm test\n", rendered)

    def test_source_and_test_roots_are_rendered_once(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "src").mkdir()
            (root / "tests").mkdir()

            rendered = project_map.render_project_map(root)

        self.assertEqual(rendered.count("- src/"), 1)
        self.assertEqual(rendered.count("- tests/"), 1)

    def test_python_project_detects_pyproject_and_verified_checks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app").mkdir()
            (root / "app" / "__init__.py").write_text("", encoding="utf-8")
            (root / "pyproject.toml").write_text(
                "[tool.pytest.ini_options]\npythonpath=['.']\n[tool.ruff]\nline-length=100\n",
                encoding="utf-8",
            )
            facts = "- successful check: python -m unittest\n- successful run: python app.py"

            rendered = project_map.render_project_map(root, facts)

        self.assertIn("pyproject.toml", rendered)
        self.assertIn("python -m pytest", rendered)
        self.assertIn("python -m ruff check .", rendered)
        self.assertIn("Observed successful checks", rendered)
        self.assertIn("successful check: python -m unittest", rendered)
        self.assertNotIn("successful run: python app.py", rendered)

    def test_empty_project_renders_minimal_map_without_fake_commands(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            rendered = project_map.render_project_map(td)

        self.assertEqual(rendered, "Project Map (bounded local scan; relative paths only):")
        self.assertNotIn("Candidate commands", rendered)

    def test_map_skips_secret_excluded_lock_and_symlink_paths(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("print('ok')\n", encoding="utf-8")
            (root / ".env").write_text("SECRET=1\n", encoding="utf-8")
            (root / "prod.env").write_text("SECRET=2\n", encoding="utf-8")
            (root / "credentials.json").write_text("{}", encoding="utf-8")
            (root / "package-lock.json").write_text("{}", encoding="utf-8")
            (root / "node_modules").mkdir()
            (root / "node_modules" / "pkg.js").write_text("leak\n", encoding="utf-8")
            link = root / "linked.py"
            try:
                link.symlink_to(root / "app.py")
            except OSError:
                link = None

            rendered = project_map.render_project_map(root)

        self.assertIn("app.py", rendered)
        self.assertNotIn(".env", rendered)
        self.assertNotIn("prod.env", rendered)
        self.assertNotIn("credentials.json", rendered)
        self.assertNotIn("package-lock.json", rendered)
        self.assertNotIn("node_modules", rendered)
        if link is not None:
            self.assertNotIn("linked.py", rendered)

    def test_large_directory_is_bounded_and_marked_truncated(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for index in range(project_map.MAX_DIRECTORY_ENTRIES + 5):
                (root / f"file_{index:03}.py").write_text("x = 1\n", encoding="utf-8")

            rendered = project_map.render_project_map(root)

        self.assertIn("map truncated", rendered)
        self.assertLessEqual(len(rendered), project_map.MAX_PROJECT_MAP_CHARS + 80)


if __name__ == "__main__":
    unittest.main()
