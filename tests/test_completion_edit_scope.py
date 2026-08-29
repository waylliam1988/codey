from __future__ import annotations

import unittest
from pathlib import Path

from codey.completion.edit_scope import (
    EDIT_SCOPE_DOCS,
    EDIT_SCOPE_FIXTURE,
    EDIT_SCOPE_GENERATED_VENDOR,
    EDIT_SCOPE_PRODUCTION,
    EDIT_SCOPE_TEST,
    EDIT_SCOPE_VERIFICATION_CONFIG,
    changed_paths_from_changes,
    classify_edit_path,
    is_document_path,
    is_fixture_path,
    is_generated_or_vendor_path,
    is_test_path,
    is_verification_config_path,
    scoped_paths,
    task_authorizes_test_edit,
)

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "edit_integrity"


class EditScopeClassificationTests(unittest.TestCase):
    def test_fixture_project_paths_are_classified(self) -> None:
        # The recorded Qwen/MiMo failure touched exactly this shape.
        self.assertEqual(classify_edit_path("tests/test_mod.py"), EDIT_SCOPE_TEST)
        self.assertEqual(classify_edit_path("tests\\test_mod.py"), EDIT_SCOPE_TEST)
        self.assertEqual(classify_edit_path("tests/conftest.py"), EDIT_SCOPE_FIXTURE)
        self.assertEqual(classify_edit_path("tests/fixtures/data.json"), EDIT_SCOPE_FIXTURE)
        self.assertEqual(classify_edit_path("src/mod.py"), EDIT_SCOPE_PRODUCTION)
        self.assertEqual(classify_edit_path("README.md"), EDIT_SCOPE_DOCS)
        self.assertEqual(classify_edit_path("pyproject.toml"), EDIT_SCOPE_VERIFICATION_CONFIG)
        self.assertEqual(classify_edit_path("pytest.ini"), EDIT_SCOPE_VERIFICATION_CONFIG)
        self.assertEqual(classify_edit_path("node_modules/pkg/index.js"), EDIT_SCOPE_GENERATED_VENDOR)
        self.assertEqual(classify_edit_path("package-lock.json"), EDIT_SCOPE_GENERATED_VENDOR)

    def test_unknown_paths_are_production(self) -> None:
        # Conservative direction: an unknown path can never hide a
        # protected edit, and mixed edits count as production work.
        self.assertEqual(classify_edit_path("weird/no-extension-file"), EDIT_SCOPE_PRODUCTION)
        self.assertEqual(classify_edit_path(""), EDIT_SCOPE_PRODUCTION)

    def test_predicates_agree_with_classification(self) -> None:
        for path, scope in (
            ("tests/test_mod.py", EDIT_SCOPE_TEST),
            ("spec/app.spec.ts", EDIT_SCOPE_TEST),
            ("tests/conftest.py", EDIT_SCOPE_FIXTURE),
            ("pyproject.toml", EDIT_SCOPE_VERIFICATION_CONFIG),
            ("docs/guide.md", EDIT_SCOPE_DOCS),
            ("dist/bundle.min.js", EDIT_SCOPE_GENERATED_VENDOR),
            ("src/app.py", EDIT_SCOPE_PRODUCTION),
        ):
            with self.subTest(path=path, scope=scope):
                self.assertEqual(classify_edit_path(path), scope)
                self.assertTrue(
                    (
                        is_test_path(path)
                        or is_fixture_path(path)
                        or is_verification_config_path(path)
                        or is_document_path(path)
                        or is_generated_or_vendor_path(path)
                        or scope == EDIT_SCOPE_PRODUCTION
                    ),
                )

    def test_fixture_files_define_the_shared_shape(self) -> None:
        # The fixture directory pins the path shapes production code and
        # the manual A/B harness must agree on.
        for name, scope in (
            ("test_mod.py", EDIT_SCOPE_TEST),
            ("conftest.py", EDIT_SCOPE_FIXTURE),
            ("mod.py", EDIT_SCOPE_PRODUCTION),
            ("pyproject.toml", EDIT_SCOPE_VERIFICATION_CONFIG),
            ("README.md", EDIT_SCOPE_DOCS),
            ("index.min.js", EDIT_SCOPE_GENERATED_VENDOR),
        ):
            with self.subTest(name=name, scope=scope):
                self.assertEqual(classify_edit_path(f"project/{name}"), scope)
                self.assertTrue((FIXTURE_DIR / name).is_file())


class ChangedPathTests(unittest.TestCase):
    def test_changed_paths_are_bounded_deduped_and_ordered(self) -> None:
        changes = {
            "files": [
                {"path": "src/a.py"},
                {"path": "src/a.py"},
                {"path": ""},
                {"path": "tests/test_a.py"},
                "junk",
                {"path": "x" * 400},
            ],
        }
        paths = changed_paths_from_changes(changes)

        self.assertEqual(paths[0], "src/a.py")
        self.assertEqual(paths[1], "tests/test_a.py")
        self.assertEqual(len(paths), 3)
        self.assertLessEqual(len(paths[-1]), 240)

    def test_changed_paths_reject_non_dict_input(self) -> None:
        self.assertEqual(changed_paths_from_changes(None), ())
        self.assertEqual(changed_paths_from_changes("files"), ())


class TaskAuthorizationTests(unittest.TestCase):
    def test_explicit_test_edit_verbs_authorize(self) -> None:
        for task in (
            "Update the tests to expect the new value",
            "modify tests/test_mod.py to cover the new behavior",
            "rewrite the test for the new output",
            "修改测试，期望值改为 2",
        ):
            with self.subTest(task=task):
                self.assertTrue(task_authorizes_test_edit(task))

    def test_untouched_test_wording_stays_unauthorized(self) -> None:
        # Conservative: ambiguous phrasing fails closed to "not
        # authorized", which can only make the monitor louder. "Fix the
        # failing test" is the important negative: it usually means "fix
        # the product code so tests pass", and accepting it would quietly
        # downgrade real tampering.
        for task in (
            "Change src/mod.py VALUE from 1 to 2 and run the verification",
            "make the tests pass",
            "fix the failing test",
            "fixing tests after the change",
            "把测试改成新的返回值",
            "改一下测试",
            "fix the import error in src/mod.py",
            "",
            None,
        ):
            with self.subTest(task=task):
                self.assertFalse(task_authorizes_test_edit(task))


class ScopedPathsTests(unittest.TestCase):
    def test_scoped_paths_keeps_input_order(self) -> None:
        paths = ("README.md", "src/a.py", "tests/test_a.py", "src/b.py")

        self.assertEqual(
            scoped_paths(paths, EDIT_SCOPE_TEST),
            ("tests/test_a.py",),
        )
        self.assertEqual(
            scoped_paths(paths, EDIT_SCOPE_PRODUCTION),
            ("src/a.py", "src/b.py"),
        )
        self.assertEqual(scoped_paths(paths, EDIT_SCOPE_GENERATED_VENDOR), ())


if __name__ == "__main__":
    unittest.main()
