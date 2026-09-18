from __future__ import annotations

import os
import stat
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from codey.workspace.paths import (
    bounded_directory_entries,
    content_hash,
    ensure_not_symlink,
    is_test_path,
    path_hash,
    read_text_bounded,
    read_text_bounded_no_follow,
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

    def test_no_follow_reader_refuses_trailing_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "real.txt"
            target.write_text("secret\n", encoding="utf-8")
            link = Path(td) / "link.txt"
            try:
                link.symlink_to(target)
            except OSError:
                self.skipTest("symlinks unavailable")
            with self.assertRaisesRegex(ValueError, "symlink"):
                read_text_bounded_no_follow(link, max_bytes=64)
            with self.assertRaisesRegex(ValueError, "symlink"):
                read_text_bounded(link, max_bytes=64)
            with self.assertRaisesRegex(ValueError, "symlink"):
                ensure_not_symlink(link)
            with self.assertRaisesRegex(ValueError, "symlink"):
                path_hash(link)

    def test_path_hash_opens_without_following_symlinks(self) -> None:
        # Proves the hash path cannot bypass the no-follow helper: a swapped-in
        # link must fail even if it points at readable content, and on POSIX
        # the open itself must carry O_NOFOLLOW.
        with tempfile.TemporaryDirectory() as td:
            victim = Path(td) / "victim.txt"
            victim.write_text("original\n", encoding="utf-8")
            self.assertEqual(path_hash(victim), content_hash("original\n"))

            other = Path(td) / "other.txt"
            other.write_text("attacker\n", encoding="utf-8")
            victim.unlink()
            try:
                victim.symlink_to(other)
            except OSError:
                self.skipTest("symlinks unavailable")
            with self.assertRaisesRegex(ValueError, "symlink"):
                path_hash(victim)

    def test_path_hash_fd_open_carries_no_follow_flag(self) -> None:
        if not hasattr(os, "O_NOFOLLOW"):
            self.skipTest("O_NOFOLLOW unavailable")
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "a.txt"
            path.write_text("hello\n", encoding="utf-8")
            seen: dict[str, int] = {}
            real_open = os.open

            def spy_open(file, flags, *args, **kwargs):
                seen["flags"] = int(flags)
                return real_open(file, flags, *args, **kwargs)

            with mock.patch.object(os, "open", spy_open):
                self.assertEqual(path_hash(path), content_hash("hello\n"))
            self.assertTrue(seen["flags"] & os.O_NOFOLLOW)

    def test_no_follow_reader_opens_with_no_follow_flag(self) -> None:
        if not hasattr(os, "O_NOFOLLOW"):
            self.skipTest("O_NOFOLLOW unavailable")
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "b.txt"
            path.write_text("hello\n", encoding="utf-8")
            seen: dict[str, int] = {}
            real_open = os.open

            def spy_open(file, flags, *args, **kwargs):
                seen["flags"] = int(flags)
                return real_open(file, flags, *args, **kwargs)

            with mock.patch.object(os, "open", spy_open):
                self.assertEqual(
                    read_text_bounded_no_follow(path, max_bytes=64), "hello\n"
                )
            self.assertTrue(seen["flags"] & os.O_NOFOLLOW)

    def test_no_follow_reader_rechecks_opened_fd_is_regular(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "c.txt"
            path.write_text("hello\n", encoding="utf-8")
            with mock.patch.object(
                os,
                "fstat",
                return_value=SimpleNamespace(st_mode=stat.S_IFDIR),
            ):
                with self.assertRaisesRegex(ValueError, "not a file"):
                    read_text_bounded_no_follow(path, max_bytes=64)
                with self.assertRaisesRegex(ValueError, "not a file"):
                    path_hash(path)


if __name__ == "__main__":
    unittest.main()
