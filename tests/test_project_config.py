from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codey.project_config import (
    MAX_PROJECT_CONFIG_BYTES,
    MAX_WARNINGS,
    MIN_PROJECT_MAP_CHARS,
    PROJECT_CONFIG_RELATIVE_PATH,
    ProjectVerificationCommand,
    load_project_config,
    normalize_project_relative_path,
    path_matches_ignored_prefix,
    render_project_config_warnings,
)


def _write_config(root: Path, data: object) -> None:
    path = root / PROJECT_CONFIG_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


class ProjectConfigTests(unittest.TestCase):
    def test_missing_config_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            result = load_project_config(Path(td))

        self.assertEqual(result.config.verification_commands, ())
        self.assertEqual(result.config.ignored_paths, ())
        self.assertEqual(result.warnings, ())

    def test_parses_bounded_project_config(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_config(root, {
                "schema_version": 1,
                "verification": {
                    "commands": [
                        {
                            "command": "python -m pytest tests/test_api.py",
                            "cwd": ".",
                            "label": "api tests",
                        }
                    ],
                },
                "scan": {"ignored_paths": ["dist/", "packages/web/generated/"]},
                "context": {"budget_hints": {"project_map_chars": 2500}},
                "providers": {"preferred": {"project": "deepseek", "review": "qwen"}},
            })

            result = load_project_config(root)

        self.assertEqual(
            result.config.verification_commands,
            (ProjectVerificationCommand("python -m pytest tests/test_api.py", ".", "api tests"),),
        )
        self.assertEqual(result.config.ignored_paths, ("dist", "packages/web/generated"))
        self.assertEqual(result.config.context_budget_hints.project_map_chars, 2500)
        self.assertEqual(
            tuple((item.mode, item.provider_id) for item in result.config.preferred_providers),
            (("project", "deepseek"), ("review", "qwen")),
        )
        self.assertEqual(result.warnings, ())

    def test_invalid_json_fails_open_with_bounded_warning(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / PROJECT_CONFIG_RELATIVE_PATH
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{not json", encoding="utf-8")

            result = load_project_config(root)

        self.assertEqual(result.config.verification_commands, ())
        self.assertIn("invalid JSON", result.warnings[0])
        self.assertIn("- ignored .codey/config.json", render_project_config_warnings(result))

    def test_large_config_is_rejected_before_reading_body(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / PROJECT_CONFIG_RELATIVE_PATH
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"{" + (b" " * MAX_PROJECT_CONFIG_BYTES) + b"}")

            with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("read body")):
                result = load_project_config(root)

        self.assertEqual(result.config.verification_commands, ())
        self.assertIn("exceeds", result.warnings[0])

    def test_project_config_import_does_not_load_browser_provider_stack(self) -> None:
        script = (
            "import sys; import codey.project_config; "
            "print('codey.browser' in sys.modules); "
            "print('codey.providers.registry' in sys.modules)"
        )
        proc = subprocess.run(
            [sys.executable, "-B", "-c", script],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip().splitlines(), ["False", "False"])

    def test_rejects_symlink_config(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td, "project")
            outside = Path(td, "outside")
            root.mkdir()
            outside.mkdir()
            config_dir = root / ".codey"
            config_dir.mkdir()
            target = outside / "config.json"
            target.write_text('{"schema_version":1}', encoding="utf-8")
            link = config_dir / "config.json"
            try:
                link.symlink_to(target)
            except OSError as exc:
                self.skipTest(f"file symlink unavailable: {exc}")

            result = load_project_config(root)

        self.assertEqual(result.config.verification_commands, ())
        self.assertIn("not a regular project file", result.warnings[0])

    def test_invalid_paths_commands_and_providers_are_warnings_not_config(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_config(root, {
                "schema_version": 1,
                "verification": {
                    "commands": [
                        {"command": "", "cwd": "."},
                        {"command": "python -m pytest", "cwd": "../escape"},
                    ],
                },
                "scan": {"ignored_paths": ["../outside", "/absolute"]},
                "context": {"budget_hints": {"project_map_chars": MIN_PROJECT_MAP_CHARS - 1}},
                "providers": {"preferred": {"project": "unknown"}},
            })

            result = load_project_config(root)

        self.assertEqual(result.config.verification_commands, ())
        self.assertEqual(result.config.ignored_paths, ())
        self.assertIsNone(result.config.context_budget_hints.project_map_chars)
        self.assertEqual(result.config.preferred_providers, ())
        self.assertGreaterEqual(len(result.warnings), 5)

    def test_warning_render_reports_omitted_warning_count(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_config(root, {
                "schema_version": 1,
                "verification": {
                    "commands": [
                        {"command": "", "cwd": "."}
                        for _ in range(MAX_WARNINGS + 3)
                    ],
                },
            })

            result = load_project_config(root)
            rendered = render_project_config_warnings(result)

        self.assertEqual(len(result.warnings), MAX_WARNINGS)
        self.assertGreater(result.warning_count, len(result.warnings))
        self.assertIn("omitted", rendered)

    def test_project_relative_path_helpers_use_root_relative_prefixes(self) -> None:
        self.assertEqual(normalize_project_relative_path("./dist/"), "dist")
        self.assertIsNone(normalize_project_relative_path("../dist"))
        self.assertIsNone(normalize_project_relative_path("/dist"))
        self.assertIsNone(normalize_project_relative_path("C:/dist"))

        ignored = ("generated", "packages/web/generated")
        self.assertTrue(path_matches_ignored_prefix("generated/client.py", ignored))
        self.assertTrue(path_matches_ignored_prefix("packages/web/generated/client.py", ignored))
        self.assertFalse(path_matches_ignored_prefix("src/generated/client.py", ignored))


if __name__ == "__main__":
    unittest.main()
