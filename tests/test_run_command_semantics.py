"""Canonical run-command semantics: filesystem operands stay project-scoped.

Policy and executor must see the same argv AND the same path semantics. These
tests pin the canonicalizer contract itself, independent of any sink.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from codey.policies.run_command_semantics import (
    RunCommandPolicyError,
    canonical_run_command,
)


class CanonicalRunCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.root = base / "project"
        (self.root / "tests").mkdir(parents=True)
        (self.root / "app.py").write_text("print('in')\n", encoding="utf-8")
        base.joinpath("outside.py").write_text("print('out')\n", encoding="utf-8")
        (base / "outside").mkdir()
        (base / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")

    @property
    def outside_abs(self) -> str:
        return (Path(self._tmp.name) / "outside.py").as_posix()

    def canonical(self, command: str, rel: str = "."):
        return canonical_run_command(self.root, rel, command)

    # --- accepted inside references -------------------------------------

    def test_accepts_inside_script_and_test_paths(self) -> None:
        for command in (
            "python app.py",
            "python -B ./app.py",
            "python tests/run.py",
            "pytest -q tests",
            "python -m pytest -q tests",
            "ruff check .",
            "mypy .",
            "mypy src/app.py",
            "npm test",
            "bun run test",
            "go test ./...",
            "cargo build",
            "dotnet test",
            "make test",
        ):
            with self.subTest(command=command):
                canonical = self.canonical(command)
                self.assertEqual(canonical.cwd, self.root.resolve())
                for ref in canonical.referenced_paths:
                    self.assertTrue(
                        self.root.resolve() in ref.resolved.parents
                        or ref.resolved == self.root.resolve(),
                        msg=f"{ref.raw} resolved outside root",
                    )

    def test_argv_matches_plain_tokenization(self) -> None:
        canonical = self.canonical("python -m pytest -q 'tests/test x.py'")
        self.assertEqual(
            list(canonical.argv),
            ["python", "-m", "pytest", "-q", "tests/test x.py"],
        )

    def test_pytest_nodeid_suffix_is_not_resolved_as_directory(self) -> None:
        canonical = self.canonical("pytest tests/test_app.py::test_case")
        self.assertEqual(canonical.referenced_paths[0].raw, "tests/test_app.py::test_case")
        self.assertTrue(
            str(canonical.referenced_paths[0].resolved).endswith("test_app.py")
        )

    # --- rejected outside references ------------------------------------

    def test_rejects_relative_escape_operands(self) -> None:
        denied = (
            "python ../outside.py",
            "python ..\\outside.py",
            f"python {self.outside_abs}",
            "python -B ../outside.py",
            "python -m py_compile ../outside.py",
            "python -m unittest discover -s ../outside",
            "pytest ../outside",
            "pytest ../outside.py",
            f"pytest {Path(self._tmp.name).as_posix()}",
            "pytest -c ../pytest.ini",
            "pytest --rootdir=..",
            "pytest --rootdir ../outside",
            "pytest --confcutdir=../outside",
            "pytest --basetemp ../outside/tmp",
            "python -m pytest --rootdir=../outside",
            "python -m pytest -c ../pytest.ini",
            "ruff check ../outside",
            "ruff --config ../outside/ruff.toml check .",
            "mypy ../outside",
            "mypy --cache-dir ../outside/.mypy_cache .",
            "python -m mypy --config-file ../outside/mypy.ini",
            "deno test --config ../outside/deno.json",
            "go test -coverprofile=../outside/cover.out ./...",
            "cargo test --manifest-path ../outside/Cargo.toml",
            "dotnet test --results-directory ../outside/results",
        )
        for command in denied:
            with self.subTest(command=command):
                with self.assertRaises(RunCommandPolicyError) as ctx:
                    self.canonical(command)
                self.assertEqual(ctx.exception.reason_code, "command_path_escape")

    def test_error_display_never_carries_raw_operand(self) -> None:
        secret_like = "../secret-token.txt"
        with self.assertRaises(RunCommandPolicyError) as ctx:
            self.canonical(f"python {secret_like}")
        self.assertNotIn(secret_like, ctx.exception.display)
        self.assertNotIn(secret_like, str(ctx.exception))

    def test_empty_project_fails_closed(self) -> None:
        with self.assertRaises(RunCommandPolicyError) as ctx:
            canonical_run_command("", ".", "python app.py")
        self.assertEqual(ctx.exception.reason_code, "project_required")

    def test_untokenizable_command_fails_closed(self) -> None:
        with self.assertRaises(RunCommandPolicyError) as ctx:
            self.canonical("python 'unterminated")
        self.assertEqual(ctx.exception.reason_code, "invalid_command")

    # --- pytest ini overrides -------------------------------------------

    def test_pytest_override_ini_path_keys_are_collected(self) -> None:
        denied = (
            "pytest -o cache_dir=../outside/cache",
            "pytest -o cache_dir ../outside/cache",
            "pytest -o=cache_dir=../outside/cache",
            "pytest --override-ini cache_dir=../outside/cache",
            "pytest --override-ini=cache_dir=../outside/cache",
            "pytest -o CACHE_DIR=../outside/cache",
            "python -m pytest -o cache_dir=../outside/cache",
        )
        for command in denied:
            with self.subTest(command=command):
                with self.assertRaises(RunCommandPolicyError) as ctx:
                    self.canonical(command)
                self.assertEqual(ctx.exception.reason_code, "command_path_escape")

    def test_pytest_override_ini_non_path_keys_pass_through(self) -> None:
        for command in (
            "pytest -o addopts=-q",
            "pytest -o junit_family=xunit2",
            "pytest --override-ini asyncio_mode=auto",
        ):
            with self.subTest(command=command):
                canonical = self.canonical(command)
                self.assertEqual(canonical.referenced_paths, ())

    def test_pytest_override_ini_inside_cache_dir_is_allowed(self) -> None:
        canonical = self.canonical("pytest -o cache_dir=.pytest_cache tests")
        cache_refs = [
            ref
            for ref in canonical.referenced_paths
            if ref.resolved.name == ".pytest_cache"
        ]
        self.assertEqual(len(cache_refs), 1)
        self.assertTrue(
            cache_refs[0].resolved.is_relative_to(self.root.resolve())
        )

    def test_python_m_pytest_parity_with_direct_pytest(self) -> None:
        for direct in (
            "pytest ../outside",
            "pytest -c ../pytest.ini",
            "pytest --rootdir=..",
            "pytest -o cache_dir=../outside/cache",
        ):
            module = direct.replace("pytest", "python -m pytest", 1)
            with self.subTest(direct=direct):
                for command in (direct, module):
                    with self.assertRaises(RunCommandPolicyError) as ctx:
                        self.canonical(command)
                    self.assertEqual(ctx.exception.reason_code, "command_path_escape")

    # --- platform-specific absolute forms -------------------------------

    @unittest.skipUnless(sys.platform.startswith("win"), "windows drive paths")
    def test_rejects_windows_drive_absolute_outside_project(self) -> None:
        for command in (
            "python C:/Windows/Temp/outside.py",
            "pytest C:/outside",
            "ruff check C:/outside",
            "mypy C:/outside",
        ):
            with self.subTest(command=command):
                with self.assertRaises(RunCommandPolicyError) as ctx:
                    self.canonical(command)
                self.assertEqual(ctx.exception.reason_code, "command_path_escape")

    @unittest.skipIf(sys.platform.startswith("win"), "posix absolute paths")
    def test_rejects_posix_absolute_outside_project(self) -> None:
        for command in (
            "python /tmp/outside.py",
            "pytest /tmp/outside",
            "ruff check /etc",
        ):
            with self.subTest(command=command):
                with self.assertRaises(RunCommandPolicyError) as ctx:
                    self.canonical(command)
                self.assertEqual(ctx.exception.reason_code, "command_path_escape")


if __name__ == "__main__":
    unittest.main()
