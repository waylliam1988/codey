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
    check_covers_selected_candidate,
    discover_verification_candidates,
    select_verification_candidate,
    selected_verification_candidate_lines,
    verification_candidate_lines,
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

    def test_package_scripts_use_priority_instead_of_becoming_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as td, mock.patch(
            "codey.verification_policy.shutil.which", return_value="executable"
        ):
            root = Path(td)
            (root / "package.json").write_text(
                (
                    '{"scripts":{'
                    '"test":"node test.js",'
                    '"lint":"eslint .",'
                    '"typecheck":"tsc --noEmit",'
                    '"check":"node check.js",'
                    '"build":"vite build"'
                    "}}"
                ),
                encoding="utf-8",
            )
            candidates = discover_verification_candidates(root)
            selected = select_verification_candidate(candidates, ("src/app.ts",))

        self.assertIsNotNone(selected)
        self.assertEqual(selected.command, "npm test")

    def test_package_test_beats_make_test_in_same_directory(self) -> None:
        with tempfile.TemporaryDirectory() as td, mock.patch(
            "codey.verification_policy.shutil.which", return_value="executable"
        ):
            root = Path(td)
            (root / "package.json").write_text(
                '{"scripts":{"test":"node test.js"}}',
                encoding="utf-8",
            )
            (root / "Makefile").write_text(
                "test:\n\t@node test.js\n",
                encoding="utf-8",
            )
            candidates = discover_verification_candidates(root)
            selected = select_verification_candidate(candidates, ("src/app.ts",))

        self.assertIsNotNone(selected)
        self.assertEqual(selected.command, "npm test")

    def test_pytest_beats_unittest_discover(self) -> None:
        with tempfile.TemporaryDirectory() as td, mock.patch(
            "codey.verification_policy.shutil.which", return_value="python"
        ):
            root = Path(td)
            (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
            (root / "tests").mkdir()
            candidates = discover_verification_candidates(root)
            selected = select_verification_candidate(candidates, ("app.py",))

        self.assertIsNotNone(selected)
        self.assertEqual(selected.command, "python -m pytest")

    def test_mypy_beats_ruff_when_only_static_checks_exist(self) -> None:
        def which(command: str) -> str | None:
            return command if command in {"mypy", "ruff"} else None

        with tempfile.TemporaryDirectory() as td, mock.patch(
            "codey.verification_policy.shutil.which", side_effect=which
        ):
            root = Path(td)
            (root / "mypy.ini").write_text("[mypy]\n", encoding="utf-8")
            (root / "ruff.toml").write_text("line-length = 88\n", encoding="utf-8")
            candidates = discover_verification_candidates(root)
            selected = select_verification_candidate(candidates, ("app.py",))

        self.assertIsNotNone(selected)
        self.assertEqual(selected.command, "mypy .")

    def test_build_only_script_can_be_selected(self) -> None:
        with tempfile.TemporaryDirectory() as td, mock.patch(
            "codey.verification_policy.shutil.which", return_value="executable"
        ):
            root = Path(td)
            (root / "package.json").write_text(
                '{"scripts":{"build":"vite build"}}',
                encoding="utf-8",
            )
            candidates = discover_verification_candidates(root)
            selected = select_verification_candidate(candidates, ("src/app.ts",))

        self.assertIsNotNone(selected)
        self.assertEqual(selected.command, "npm run build")

    def test_package_manager_field_beats_lockfiles(self) -> None:
        with tempfile.TemporaryDirectory() as td, mock.patch(
            "codey.verification_policy.shutil.which", return_value="executable"
        ):
            root = Path(td)
            (root / "pnpm-lock.yaml").write_text("lockfileVersion: '9'\n", encoding="utf-8")
            (root / "package.json").write_text(
                '{"packageManager":"yarn@4.0.0","scripts":{"test":"vitest"}}',
                encoding="utf-8",
            )
            candidates = discover_verification_candidates(root)

        self.assertTrue(any(item.command == "yarn test" for item in candidates))
        self.assertFalse(any(item.command == "pnpm test" for item in candidates))

    def test_nearest_parent_lockfile_selects_package_manager_for_monorepo(self) -> None:
        with tempfile.TemporaryDirectory() as td, mock.patch(
            "codey.verification_policy.shutil.which", return_value="executable"
        ):
            root = Path(td)
            (root / "pnpm-lock.yaml").write_text("lockfileVersion: '9'\n", encoding="utf-8")
            frontend = root / "frontend"
            frontend.mkdir()
            (frontend / "package.json").write_text(
                '{"scripts":{"test":"vitest"}}',
                encoding="utf-8",
            )
            candidates = discover_verification_candidates(root)
            selected = select_verification_candidate(candidates, ("frontend/src/app.ts",))

        self.assertIsNotNone(selected)
        self.assertEqual(selected.command, "pnpm test")
        self.assertEqual(selected.cwd, "frontend")

    def test_current_directory_lockfile_beats_parent_lockfile(self) -> None:
        with tempfile.TemporaryDirectory() as td, mock.patch(
            "codey.verification_policy.shutil.which", return_value="executable"
        ):
            root = Path(td)
            (root / "pnpm-lock.yaml").write_text("lockfileVersion: '9'\n", encoding="utf-8")
            frontend = root / "frontend"
            frontend.mkdir()
            (frontend / "yarn.lock").write_text("", encoding="utf-8")
            (frontend / "package.json").write_text(
                '{"scripts":{"test":"vitest"}}',
                encoding="utf-8",
            )
            candidates = discover_verification_candidates(root)

        self.assertTrue(any(item.command == "yarn test" for item in candidates))

    def test_bun_package_scripts_use_bun_run(self) -> None:
        with tempfile.TemporaryDirectory() as td, mock.patch(
            "codey.verification_policy.shutil.which", return_value="executable"
        ):
            root = Path(td)
            (root / "bun.lockb").write_text("", encoding="utf-8")
            (root / "package.json").write_text(
                '{"scripts":{"test":"bun test","lint":"eslint ."}}',
                encoding="utf-8",
            )
            candidates = discover_verification_candidates(root)

        self.assertIn(
            VerificationCandidate("bun run test", ".", "package.json script test"),
            candidates,
        )
        self.assertIn(
            VerificationCandidate("bun run lint", ".", "package.json script lint"),
            candidates,
        )

    @mock.patch("codey.verification_policy.shutil.which", return_value="bun")
    def test_bun_test_covers_bun_run_test_candidate(self, _which) -> None:
        candidate = VerificationCandidate("bun run test", ".")

        self.assertTrue(
            check_covers_selected_candidate(
                candidate,
                "bun test",
                ".",
                ("src/app.ts",),
            )
        )

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

    def test_historical_node_managers_require_current_script(self) -> None:
        commands = (
            VerifiedCommand("pnpm test", "."),
            VerifiedCommand("yarn run lint", "."),
            VerifiedCommand("bun run typecheck", "."),
        )
        with tempfile.TemporaryDirectory() as td, mock.patch(
            "codey.verification_policy.shutil.which", return_value="executable"
        ):
            root = Path(td)
            package = root / "package.json"
            package.write_text(
                '{"scripts":{"test":"vitest","lint":"eslint .","typecheck":"tsc --noEmit"}}',
                encoding="utf-8",
            )
            current = discover_verification_candidates(root, commands)
            package.write_text('{"scripts":{"test":"vitest"}}', encoding="utf-8")
            stale = discover_verification_candidates(root, commands)

        self.assertTrue(any(item.command == "pnpm test" for item in current))
        self.assertTrue(any(item.command == "yarn run lint" for item in current))
        self.assertTrue(any(item.command == "bun run typecheck" for item in current))
        self.assertTrue(any(item.command == "pnpm test" for item in stale))
        self.assertFalse(any(item.command == "yarn run lint" for item in stale))
        self.assertFalse(any(item.command == "bun run typecheck" for item in stale))

    def test_historical_bun_test_does_not_require_package_script(self) -> None:
        command = VerifiedCommand("bun test", ".")
        with tempfile.TemporaryDirectory() as td, mock.patch(
            "codey.verification_policy.shutil.which", return_value="bun"
        ):
            root = Path(td)
            (root / "bun.lockb").write_text("", encoding="utf-8")
            current = discover_verification_candidates(root, (command,))
            (root / "bun.lockb").unlink()
            stale = discover_verification_candidates(root, (command,))

        self.assertTrue(any(item.command == "bun test" for item in current))
        self.assertFalse(any(item.command == "bun test" for item in stale))

    def test_pyproject_pytest_ini_options_is_discovered(self) -> None:
        with tempfile.TemporaryDirectory() as td, mock.patch(
            "codey.verification_policy.shutil.which", return_value="python"
        ):
            root = Path(td)
            (root / "pyproject.toml").write_text(
                "[tool.pytest.ini_options]\npythonpath=['.']\n",
                encoding="utf-8",
            )
            candidates = discover_verification_candidates(root)

        self.assertIn(
            VerificationCandidate("python -m pytest", ".", "tool.pytest"),
            candidates,
        )

    def test_tests_directory_discovers_unittest(self) -> None:
        with tempfile.TemporaryDirectory() as td, mock.patch(
            "codey.verification_policy.shutil.which", return_value="python"
        ):
            root = Path(td)
            (root / "tests").mkdir()
            candidates = discover_verification_candidates(root)

        self.assertIn(
            VerificationCandidate("python -m unittest discover", ".", "tests directory"),
            candidates,
        )

    def test_ruff_and_mypy_configs_are_discovered(self) -> None:
        def which(command: str) -> str | None:
            return command if command in {"mypy", "ruff"} else None

        with tempfile.TemporaryDirectory() as td, mock.patch(
            "codey.verification_policy.shutil.which", side_effect=which
        ):
            root = Path(td)
            (root / "pyproject.toml").write_text(
                "[tool.ruff]\nline-length=88\n\n[tool.mypy]\nstrict=true\n",
                encoding="utf-8",
            )
            candidates = discover_verification_candidates(root)

        self.assertIn(VerificationCandidate("ruff check .", ".", "tool.ruff"), candidates)
        self.assertIn(VerificationCandidate("mypy .", ".", "tool.mypy"), candidates)

    def test_makefile_discovers_only_safe_targets(self) -> None:
        with tempfile.TemporaryDirectory() as td, mock.patch(
            "codey.verification_policy.shutil.which", return_value="make"
        ):
            root = Path(td)
            (root / "Makefile").write_text(
                (
                    ".PHONY: test deploy\n"
                    "test:\n\tpytest\n"
                    "deploy:\n\tship\n"
                    "lint:\n\truff check .\n"
                    "%.o: %.c\n\tcc -c $<\n"
                ),
                encoding="utf-8",
            )
            candidates = discover_verification_candidates(root)

        self.assertIn(VerificationCandidate("make test", ".", "Makefile target test"), candidates)
        self.assertIn(VerificationCandidate("make lint", ".", "Makefile target lint"), candidates)
        self.assertFalse(any(item.command == "make deploy" for item in candidates))

    def test_makefile_assignment_is_not_discovered_as_target(self) -> None:
        with tempfile.TemporaryDirectory() as td, mock.patch(
            "codey.verification_policy.shutil.which", return_value="make"
        ):
            root = Path(td)
            (root / "Makefile").write_text(
                "check := ruff check .\nlint ::= ruff check .\ntest:\n\tpytest\n",
                encoding="utf-8",
            )
            candidates = discover_verification_candidates(root)

        self.assertIn(VerificationCandidate("make test", ".", "Makefile target test"), candidates)
        self.assertFalse(any(item.command == "make check" for item in candidates))
        self.assertFalse(any(item.command == "make lint" for item in candidates))

    def test_makefile_targets_use_priority_instead_of_becoming_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as td, mock.patch(
            "codey.verification_policy.shutil.which", return_value="make"
        ):
            root = Path(td)
            (root / "Makefile").write_text(
                "lint:\n\truff check .\ntest:\n\tpytest\n",
                encoding="utf-8",
            )
            candidates = discover_verification_candidates(root)
            selected = select_verification_candidate(candidates, ("app.py",))

        self.assertIsNotNone(selected)
        self.assertEqual(selected.command, "make test")

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

    @mock.patch("codey.verification_policy.shutil.which", return_value="python")
    def test_full_pytest_can_cover_unittest_fallback(self, _which) -> None:
        candidate = VerificationCandidate("python -m unittest discover", ".")
        self.assertTrue(
            check_covers_selected_candidate(
                candidate,
                "python -m pytest",
                ".",
                ("src/app.py",),
            )
        )

    @mock.patch("codey.verification_policy.shutil.which", return_value="python")
    def test_pytest_output_flags_can_cover_unittest_fallback(self, _which) -> None:
        candidate = VerificationCandidate("python -m unittest discover", ".")
        self.assertTrue(
            check_covers_selected_candidate(
                candidate,
                "python -m pytest -q -ra",
                ".",
                ("src/app.py",),
            )
        )

    @mock.patch("codey.verification_policy.shutil.which", return_value="python")
    def test_pytest_filter_flags_do_not_cover_unittest_fallback(self, _which) -> None:
        candidate = VerificationCandidate("python -m unittest discover", ".")
        for command in (
            "python -m pytest --ignore=src",
            "pytest --ignore=src",
            "python -m pytest -k test_value",
            "python -m pytest -m slow",
            "python -m pytest --collect-only",
        ):
            with self.subTest(command=command):
                self.assertFalse(
                    check_covers_selected_candidate(
                        candidate,
                        command,
                        ".",
                        ("src/app.py",),
                    )
                )

    @mock.patch("codey.verification_policy.shutil.which", return_value="python")
    def test_scoped_pytest_does_not_cover_unrelated_changed_file(self, _which) -> None:
        candidate = VerificationCandidate("python -m unittest discover", ".")
        self.assertFalse(
            check_covers_selected_candidate(
                candidate,
                "python -m pytest tests/old.py",
                ".",
                ("src/app.py",),
            )
        )

    @mock.patch("codey.verification_policy.shutil.which", return_value="python")
    def test_py_compile_does_not_substitute_for_pytest_candidate(self, _which) -> None:
        candidate = VerificationCandidate("python -m pytest", ".")
        self.assertFalse(
            check_covers_selected_candidate(
                candidate,
                "python -m py_compile other.py",
                ".",
                ("app.py",),
            )
        )

    def test_green_check_must_exactly_match_selected_candidate(self) -> None:
        candidate = VerificationCandidate("python -m pytest", "pkg")
        self.assertTrue(check_matches_candidate(candidate, "python -m pytest", "pkg"))
        self.assertTrue(check_matches_candidate(candidate, "python -m pytest", "pkg/."))
        self.assertFalse(
            check_matches_candidate(candidate, "python -m py_compile other.py", "pkg")
        )
        self.assertFalse(check_matches_candidate(candidate, "python -m pytest", "."))

    def test_verification_candidate_lines_collapse_embedded_newlines(self) -> None:
        lines = verification_candidate_lines((
            VerificationCandidate("python -m pytest\nInjected:", "pkg\rsub"),
        ))

        self.assertEqual(lines, ("pkg sub/: python -m pytest Injected:",))
        self.assertFalse(any("\n" in line or "\r" in line for line in lines))

    def test_selected_verification_candidate_lines_returns_only_selected_candidate(self) -> None:
        lines = selected_verification_candidate_lines(
            (
                VerificationCandidate("python -m pytest", "."),
                VerificationCandidate("python -m pytest", "backend"),
                VerificationCandidate("pnpm test", "frontend"),
            ),
            ("backend/app.py",),
        )

        self.assertEqual(lines, ("backend/: python -m pytest",))

    def test_verification_candidate_lines_keeps_all_candidates_with_selected_first(self) -> None:
        lines = verification_candidate_lines(
            (
                VerificationCandidate("python -m pytest", "."),
                VerificationCandidate("python -m pytest", "backend"),
                VerificationCandidate("pnpm test", "frontend"),
            ),
            ("backend/app.py",),
        )

        self.assertEqual(
            lines,
            (
                "backend/: python -m pytest",
                "python -m pytest",
                "frontend/: pnpm test",
            ),
        )

    def test_selected_verification_candidate_lines_empty_when_policy_has_no_unique_match(
        self,
    ) -> None:
        lines = selected_verification_candidate_lines(
            (
                VerificationCandidate("python -m pytest", "backend"),
                VerificationCandidate("pytest", "backend"),
            ),
            ("backend/app.py",),
        )

        self.assertEqual(lines, ())

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
