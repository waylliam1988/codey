from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from codey.workspace.paths import (
    bounded_directory_entries,
    content_hash,
    is_test_path,
    path_hash,
    read_text_bounded,
    read_text_or_none,
    safe_join,
)


class WorkspacePathTests(unittest.TestCase):
    def test_safe_join_rejects_root_escape_with_domain_label(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            with self.assertRaisesRegex(ValueError, "escapes knowledge root"):
                safe_join(root, "../escape.txt", label="knowledge root")

    def test_text_and_hash_helpers_share_snapshot_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "note.md"
            path.write_text("body\n", encoding="utf-8")

            self.assertEqual(read_text_or_none(path, max_bytes=64), "body\n")
            self.assertIsNone(read_text_or_none(Path(td) / "missing.md", max_bytes=64))
            self.assertEqual(path_hash(path), content_hash("body\n"))
            self.assertEqual(path_hash(Path(td) / "missing.md"), "missing")
            with self.assertRaisesRegex(ValueError, "file too large"):
                read_text_bounded(path, max_bytes=1)

    def test_bounded_directory_entries_consumes_one_probe_entry(self) -> None:
        class FakeEntry:
            def __init__(self, name: str) -> None:
                self.name = name

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

        entries, truncated = bounded_directory_entries(
            directory, 3, sort_key=lambda item: item.name  # type: ignore[arg-type]
        )

        self.assertTrue(truncated)
        self.assertEqual(directory.seen, 4)
        self.assertEqual([entry.name for entry in entries], ["file_0.py", "file_1.py", "file_2.py"])

    def test_bounded_directory_entries_can_skip_hidden_entries(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / ".env").write_text("SECRET=1\n", encoding="utf-8")
            (root / "app.py").write_text("print('ok')\n", encoding="utf-8")

            entries, truncated = bounded_directory_entries(root, 10, include_hidden=False)

        self.assertFalse(truncated)
        self.assertEqual([entry.name for entry in entries], ["app.py"])

    def test_is_test_path_covers_python_and_javascript_conventions(self) -> None:
        self.assertTrue(is_test_path("tests/test_api.py"))
        self.assertTrue(is_test_path("src/__tests__/router.ts"))
        self.assertTrue(is_test_path("src/router.test.ts"))
        self.assertTrue(is_test_path("src/routes.spec.ts"))
        self.assertTrue(is_test_path("src/session_test.py"))
        self.assertFalse(is_test_path("src/latest.py"))


if __name__ == "__main__":
    unittest.main()
