from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

from codey.atomic_io import encode_with_original_eol, write_text_atomic


class AtomicWriteTests(unittest.TestCase):
    def test_write_is_atomic_and_replaces_content(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td, "app.py")
            path.write_text("before\n", encoding="utf-8")

            write_text_atomic(path, "after\n")

            self.assertEqual(path.read_text(encoding="utf-8"), "after\n")
            leftovers = [item.name for item in Path(td).iterdir() if item.name != "app.py"]
            self.assertEqual(leftovers, [])

    def test_creates_missing_parents(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td, "nested", "dir", "new.py")

            write_text_atomic(path, "x = 1\n")

            self.assertEqual(path.read_text(encoding="utf-8"), "x = 1\n")

    def test_stale_fixed_temp_file_is_not_reused(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td, "app.py")
            path.write_text("before\n", encoding="utf-8")
            stale = Path(td, ".app.py.atomic-tmp")
            stale.write_text("stale\n", encoding="utf-8")

            write_text_atomic(path, "after\n")

            self.assertEqual(path.read_text(encoding="utf-8"), "after\n")
            self.assertEqual(stale.read_text(encoding="utf-8"), "stale\n")

    def test_crlf_files_keep_crlf_on_rewrite(self) -> None:
        # Windows EOL regression: universal-newline reads meant editing one
        # line of a CRLF file silently rewrote the whole file to LF. The
        # writer must preserve the file's recorded newline style.
        with tempfile.TemporaryDirectory() as td:
            crlf_path = Path(td, "crlf.py")
            crlf_path.write_bytes(b"line1\r\nline2\r\n")

            write_text_atomic(crlf_path, "line1\nline2 edited\n")

            raw = crlf_path.read_bytes()
            self.assertIn(b"\r\n", raw)
            self.assertNotIn(b"line1\n", raw)

            lf_path = Path(td, "lf.py")
            lf_path.write_bytes(b"one\ntwo\n")

            write_text_atomic(lf_path, "one\ntwo edited\n")

            self.assertNotIn(b"\r\n", lf_path.read_bytes())

    @unittest.skipIf(os.name == "nt", "POSIX executable bits are not stable on Windows")
    def test_existing_file_mode_is_preserved_on_replace(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            script = Path(td, "run.sh")
            script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            os.chmod(script, 0o755)

            write_text_atomic(script, "#!/bin/sh\nexit 1\n")

            self.assertEqual(stat.S_IMODE(script.stat().st_mode), 0o755)

    def test_encode_helper_defaults_to_lf_without_a_recorded_style(self) -> None:
        self.assertEqual(
            encode_with_original_eol(Path("nonexistent-target.py"), "a\nb\n"),
            b"a\nb\n",
        )

    def test_original_file_survives_write_failure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            original = Path(td, "keep.py")
            original.write_text("original\n", encoding="utf-8")
            # A directory cannot be replaced by os.replace with a file, so
            # the final rename fails after the temp write succeeds.
            target_directory = Path(td, "target_dir")
            target_directory.mkdir()

            with self.assertRaises(OSError):
                write_text_atomic(target_directory, "replacement\n")

            self.assertEqual(original.read_text(encoding="utf-8"), "original\n")


if __name__ == "__main__":
    unittest.main()
