import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codey.project_facts import VerifiedCommand
from codey.verification_policy import (
    VerificationCandidate,
    _bounded_directories,
    _is_manifest_file,
    _read_manifest,
    check_matches_candidate,
    check_covers_changes,
    discover_verification_candidates,
    select_verification_candidate,
)


class VerificationPolicyTests(unittest.TestCase):
    def test_manifest_helpers_reject_symlinks_without_following_them(self) -> None:
        path = mock.Mock()
        path.is_symlink.return_value = True

        self.assertEqual(_read_manifest(path), "")
        self.assertFalse(_is_manifest_file(path))
        path.stat.assert_not_called()
        path.read_text.assert_not_called()
        path.is_file.assert_not_called()

    def test_read_manifest_rejects_non_regular_file_before_stat_or_read(self) -> None:
        path = mock.Mock()
        path.is_symlink.return_value = False
        path.is_file.return_value = False

        self.assertEqual(_read_manifest(path), "")
        path.stat.assert_not_called()
        path.read_text.assert_not_called()

    def test_discovers_only_explicit_runnable_manifests(self) -> None:
        with tempfile.TemporaryDirectory() as td, mock.patch(
            "codey.verification_policy.shutil.which", return_value="executable"
        ):
            root = Path(td)
            (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
            frontend = root / "frontend"
            frontend.mkdir()
            (frontend / "package.json").write_text(
                '{"scripts":{"test":"vitest","start":"vite"}}', encoding="utf-8"
            )

            candidates = discover_verification_candidates(root)

        self.assertIn(
            VerificationCandidate("python -m pytest", ".", "pytest.ini"), candidates
        )
        self.assertIn(
            VerificationCandidate("npm test", "frontend", "package.json script test"),
            candidates,
        )
        self.assertFalse(any("start" in item.command for item in candidates))

    def test_unavailable_executable_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as td, mock.patch(
            "codey.verification_policy.shutil.which", return_value=None
        ):
            root = Path(td)
            (root / "Cargo.toml").write_text("[package]\nname='x'\n", encoding="utf-8")
            self.assertEqual(discover_verification_candidates(root), ())

    def test_verified_command_still_requires_ecosystem_compatibility(self) -> None:
        with tempfile.TemporaryDirectory() as td, mock.patch(
            "codey.verification_policy.shutil.which", return_value="python"
        ):
            root = Path(td)
            candidate = discover_verification_candidates(
                root,
                (VerifiedCommand("python -m pytest", "."),),
            )[0]

        self.assertIsNone(select_verification_candidate((candidate,), ("app.ts",)))
        self.assertIs(candidate, select_verification_candidate((candidate,), ("app.py",)))

    def test_historical_npm_command_requires_current_script(self) -> None:
        command = VerifiedCommand("npm test", ".")
        with tempfile.TemporaryDirectory() as td, mock.patch(
            "codey.verification_policy.shutil.which", return_value="npm"
        ):
            root = Path(td)
            package = root / "package.json"
            package.write_text(
                '{"scripts":{"test":"node test.js"}}', encoding="utf-8"
            )
            current = discover_verification_candidates(root, (command,))
            package.write_text('{"scripts":{}}', encoding="utf-8")
            stale = discover_verification_candidates(root, (command,))

        self.assertTrue(any(item.command == "npm test" for item in current))
        self.assertFalse(any(item.command == "npm test" for item in stale))

    def test_historical_cargo_and_go_commands_require_current_manifests(self) -> None:
        commands = (
            VerifiedCommand("cargo test", "."),
            VerifiedCommand("go test ./...", "."),
        )
        with tempfile.TemporaryDirectory() as td, mock.patch(
            "codey.verification_policy.shutil.which", return_value="executable"
        ):
            root = Path(td)
            self.assertEqual(discover_verification_candidates(root, commands), ())
            (root / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
            (root / "go.mod").write_text("module example\n", encoding="utf-8")
            current = discover_verification_candidates(root, commands)

        self.assertEqual({item.command for item in current}, {"cargo test", "go test ./..."})

    def test_manifest_symlinks_are_not_verification_facts(self) -> None:
        with tempfile.TemporaryDirectory() as td, mock.patch(
            "codey.verification_policy.shutil.which", return_value="executable"
        ):
            base = Path(td)
            root = base / "project"
            outside = base / "outside"
            root.mkdir()
            outside.mkdir()
            contents = {
                "package.json": '{"scripts":{"test":"node test.js"}}',
                "pytest.ini": "[pytest]\n",
                "pyproject.toml": "[tool.pytest.ini_options]\n",
                "Cargo.toml": "[package]\nname='outside'\n",
                "go.mod": "module outside\n",
            }
            for name, content in contents.items():
                target = outside / name
                target.write_text(content, encoding="utf-8")
                try:
                    (root / name).symlink_to(target)
                except OSError as exc:
                    self.skipTest(f"file symlink unavailable: {exc}")

            candidates = discover_verification_candidates(
                root,
                (
                    VerifiedCommand("npm test", "."),
                    VerifiedCommand("cargo test", "."),
                    VerifiedCommand("go test ./...", "."),
                ),
            )

        self.assertEqual(candidates, ())

    def test_selects_nearest_unique_candidate_and_skips_docs(self) -> None:
        root = VerificationCandidate("python -m pytest", ".", "pytest.ini")
        nested = VerificationCandidate("python -m pytest", "pkg", "pytest.ini")
        self.assertIs(
            nested,
            select_verification_candidate((root, nested), ("pkg/app.py", "README.md")),
        )
        self.assertIsNone(select_verification_candidate((root,), ("README.md",)))

    @mock.patch("codey.verification_policy.shutil.which", return_value="python")
    def test_successful_check_must_cover_all_code_changes(self, _which) -> None:
        self.assertTrue(
            check_covers_changes("python -m pytest", "pkg", ("pkg/app.py",))
        )
        self.assertFalse(
            check_covers_changes("python -m pytest", "pkg", ("other/app.py",))
        )
        self.assertFalse(
            check_covers_changes("python -m pytest", ".", ("frontend/app.ts",))
        )

    def test_green_check_must_exactly_match_selected_candidate(self) -> None:
        candidate = VerificationCandidate("python -m pytest", "pkg")
        self.assertTrue(check_matches_candidate(candidate, "python -m pytest", "pkg"))
        self.assertTrue(check_matches_candidate(candidate, "python -m pytest", "pkg/."))
        self.assertFalse(
            check_matches_candidate(candidate, "python -m py_compile other.py", "pkg")
        )
        self.assertFalse(check_matches_candidate(candidate, "python -m pytest", "."))

    def test_directory_scan_has_entry_budget_and_skips_dot_directories(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for name in ("one", "two", "three", ".hidden", "NODE_MODULES"):
                (root / name).mkdir()
            with mock.patch("codey.verification_policy.MAX_SCAN_ENTRIES", 2):
                directories = tuple(_bounded_directories(root))

        self.assertLessEqual(len(directories), 3)
        self.assertFalse(any(item.name == ".hidden" for item in directories))
        self.assertFalse(any(item.name == "NODE_MODULES" for item in directories))


if __name__ == "__main__":
    unittest.main()
