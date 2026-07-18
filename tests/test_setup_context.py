from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codey.setup_context import (
    render_setup_context,
    safe_setup_context,
)


class SetupContextTests(unittest.TestCase):
    def test_renders_tools_and_manifests_without_absolute_tool_paths(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "package.json").write_text(
                json.dumps({
                    "scripts": {
                        "test": "vitest",
                        "build": "vite build",
                        "dev": "vite --host 127.0.0.1",
                    }
                }),
                encoding="utf-8",
            )
            (root / "requirements.txt").write_text("pytest\n", encoding="utf-8")

            def fake_which(name: str) -> str | None:
                if name in {"git", "python", "npm"}:
                    return f"C:/Tools/{name}.exe"
                return None

            with mock.patch("codey.setup_context.shutil.which", side_effect=fake_which):
                rendered = render_setup_context(root)

        self.assertIn("Setup Context", rendered)
        self.assertIn("- git: available", rendered)
        self.assertIn("- node: missing", rendered)
        self.assertIn("package.json: scripts test, build, dev", rendered)
        self.assertIn("requirements.txt: Python requirements", rendered)
        self.assertIn("package.json: npm install should use the project root", rendered)
        self.assertIn("requirements.txt: Python dependency install should reference this manifest", rendered)
        self.assertNotIn("C:/Tools", rendered)

    def test_nested_manifest_notes_are_scoped_to_relative_directory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "frontend").mkdir()
            (root / "frontend" / "package.json").write_text(
                json.dumps({"scripts": {"test": "vitest"}}),
                encoding="utf-8",
            )
            (root / "backend").mkdir()
            (root / "backend" / "requirements.txt").write_text("pytest\n", encoding="utf-8")

            rendered = render_setup_context(root)

        self.assertIn("frontend/package.json: scripts test", rendered)
        self.assertIn("frontend/package.json: npm install should use frontend/", rendered)
        self.assertIn("backend/requirements.txt: Python dependency install", rendered)
        self.assertIn("backend/ as the working directory", rendered)
        self.assertNotIn("python -m pip install -r requirements.txt", rendered)

    def test_node_setup_notes_use_package_manager_and_parent_lockfile(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "pnpm-lock.yaml").write_text("lockfileVersion: '9'\n", encoding="utf-8")
            frontend = root / "frontend"
            frontend.mkdir()
            (frontend / "package.json").write_text(
                json.dumps({"scripts": {"test": "vitest"}}),
                encoding="utf-8",
            )
            admin = root / "admin"
            admin.mkdir()
            (admin / "package.json").write_text(
                json.dumps({"packageManager": "yarn@4.0.0"}),
                encoding="utf-8",
            )

            rendered = render_setup_context(root)

        self.assertIn("frontend/package.json: pnpm install should use frontend/", rendered)
        self.assertIn("admin/package.json: yarn install should use admin/", rendered)
        self.assertNotIn("package install commands", rendered)

    def test_node_setup_notes_keep_npm_ci_for_local_package_lock(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "package.json").write_text("{}", encoding="utf-8")
            (root / "package-lock.json").write_text("{}", encoding="utf-8")

            rendered = render_setup_context(root)

        self.assertIn("package.json: npm ci or npm install should use the project root", rendered)

    def test_sensitive_manifest_paths_are_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / ".private").mkdir()
            (root / ".private" / "requirements.txt").write_text("secret\n", encoding="utf-8")
            (root / "secrets").mkdir()
            (root / "secrets" / "package.json").write_text("{}", encoding="utf-8")
            (root / "app").mkdir()
            (root / "app" / "package.json").write_text("{}", encoding="utf-8")

            rendered = render_setup_context(root)

        self.assertIn("app/package.json", rendered)
        self.assertNotIn(".private", rendered)
        self.assertNotIn("secrets", rendered)

    def test_sensitive_directories_do_not_consume_scan_budget(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            current = root / ".private"
            for index in range(20):
                current = current / f"nested_{index:02d}"
                current.mkdir(parents=True)
                (current / "requirements.txt").write_text("secret\n", encoding="utf-8")
            (root / "app").mkdir()
            (root / "app" / "package.json").write_text("{}", encoding="utf-8")

            with mock.patch("codey.setup_context.MAX_SETUP_SCAN_DIRS", 3):
                rendered = render_setup_context(root)

        self.assertIn("app/package.json", rendered)
        self.assertNotIn(".private", rendered)

    def test_listing_cap_reports_omitted_manifests(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for index in range(30):
                package_dir = root / f"pkg_{index:02d}"
                package_dir.mkdir()
                (package_dir / "package.json").write_text("{}", encoding="utf-8")

            rendered = render_setup_context(root)

        self.assertIn("pkg_00/package.json", rendered)
        self.assertIn("pkg_23/package.json", rendered)
        self.assertNotIn("pkg_24/package.json", rendered)
        self.assertIn("listed first 24 manifest or lockfile entries", rendered)

    def test_bad_manifest_degrades_without_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "package.json").write_text("{not-json", encoding="utf-8")

            rendered = safe_setup_context(root)

        self.assertIn("package.json: invalid JSON", rendered)

    def test_render_is_explicit_not_task_gated(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "package.json").write_text("{}", encoding="utf-8")

            rendered = render_setup_context(root)

        self.assertIn("Setup Context", rendered)
        self.assertIn("package.json", rendered)


if __name__ == "__main__":
    unittest.main()
