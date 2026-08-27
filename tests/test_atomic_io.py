from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codey.storage.atomic_io import encode_with_original_eol, write_text_atomic


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

    def test_failed_replace_cleans_read_only_temp_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td, "readonly.txt")
            target.write_text("before\n", encoding="utf-8")
            os.chmod(target, stat.S_IREAD)
            try:
                with self.assertRaises(PermissionError):
                    with mock.patch(
                        "codey.storage.atomic_io.os.replace",
                        side_effect=PermissionError("read-only target"),
                    ):
                        write_text_atomic(target, "after\n")

                leftovers = [
                    item.name
                    for item in Path(td).iterdir()
                    if item.name.startswith(".readonly.txt.") and item.name.endswith(".tmp")
                ]
                self.assertEqual(leftovers, [])
                self.assertEqual(target.read_text(encoding="utf-8"), "before\n")
            finally:
                try:
                    os.chmod(target, stat.S_IREAD | stat.S_IWRITE)
                except OSError:
                    pass

    def test_write_bytes_atomic(self) -> None:
        from codey.storage.atomic_io import write_bytes_atomic

        with tempfile.TemporaryDirectory() as td:
            path = Path(td, "data.bin")
            write_bytes_atomic(path, b"\x00\x01\x02")
            self.assertEqual(path.read_bytes(), b"\x00\x01\x02")

    def test_write_json_atomic(self) -> None:
        from codey.storage.atomic_io import write_json_atomic
        import json

        with tempfile.TemporaryDirectory() as td:
            path = Path(td, "data.json")
            write_json_atomic(path, {"key": "value"})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"key": "value"})

    @unittest.skipIf(os.name == "nt", "POSIX permission bits are not supported on Windows")
    def test_explicit_mode_is_applied_to_file(self) -> None:
        from codey.storage.atomic_io import write_json_atomic

        with tempfile.TemporaryDirectory() as td:
            secret = Path(td, "secret.json")
            write_json_atomic(secret, {"api_key": "SECRET_TOKEN"}, mode=0o600)
            self.assertEqual(stat.S_IMODE(secret.stat().st_mode), 0o600)

    def test_explicit_mode_failure_is_hard_failure(self) -> None:
        from codey.storage import atomic_io

        with tempfile.TemporaryDirectory() as td:
            secret = Path(td, "secret.json")
            secret.write_text("before\n", encoding="utf-8")
            patches = [
                mock.patch(
                    "codey.storage.atomic_io.os.chmod",
                    side_effect=PermissionError("chmod denied"),
                )
            ]
            if hasattr(atomic_io.os, "fchmod"):
                patches.append(
                    mock.patch(
                        "codey.storage.atomic_io.os.fchmod",
                        side_effect=PermissionError("fchmod denied"),
                    )
                )

            with patches[0]:
                if len(patches) == 1:
                    with self.assertRaises(PermissionError):
                        atomic_io.write_text_atomic(secret, "after\n", mode=0o600)
                else:
                    with patches[1]:
                        with self.assertRaises(PermissionError):
                            atomic_io.write_text_atomic(secret, "after\n", mode=0o600)

            self.assertEqual(secret.read_text(encoding="utf-8"), "before\n")

    def test_preserve_mode_failure_is_best_effort(self) -> None:
        from codey.storage import atomic_io

        with tempfile.TemporaryDirectory() as td:
            path = Path(td, "state.json")
            path.write_text("before\n", encoding="utf-8")
            patches = [
                mock.patch(
                    "codey.storage.atomic_io.os.chmod",
                    side_effect=PermissionError("chmod denied"),
                )
            ]
            if hasattr(atomic_io.os, "fchmod"):
                patches.append(
                    mock.patch(
                        "codey.storage.atomic_io.os.fchmod",
                        side_effect=PermissionError("fchmod denied"),
                    )
                )

            with patches[0]:
                if len(patches) == 1:
                    atomic_io.write_text_atomic(path, "after\n", preserve_mode=True)
                else:
                    with patches[1]:
                        atomic_io.write_text_atomic(path, "after\n", preserve_mode=True)

            self.assertEqual(path.read_text(encoding="utf-8"), "after\n")


if __name__ == "__main__":
    unittest.main()
