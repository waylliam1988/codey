from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codey.policies.action import (
    MAX_MANAGED_OUTPUT_BYTES,
    MAX_MANAGED_OUTPUTS_PER_RUN,
)
from codey.runtime.core.cancellation import CapturedProcess
from codey.storage.managed_outputs import (
    ManagedOutputStore,
    run_command_with_managed_output,
)


class ManagedOutputStoreTests(unittest.TestCase):
    def test_write_run_output_creates_text_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = ManagedOutputStore(td)

            ref = store.write_run_output(
                session_id="session-1",
                run_id="run-1",
                tool_id="2:0",
                permission_profile="coding_writer",
                command="python -m pytest -q",
                cwd=".",
                text="full output\n",
            )

            self.assertIsNotNone(ref)
            assert ref is not None
            self.assertTrue(ref.path.is_file())
            self.assertEqual(ref.path.read_text(encoding="utf-8"), "full output\n")
            metadata = json.loads(
                store.metadata_path_for("session-1", "run-1", ref.handle).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(metadata["handle"], ref.handle)
            self.assertEqual(metadata["tool_id"], "2:0")
            self.assertEqual(metadata["original_bytes"], len(b"full output\n"))
            self.assertEqual(metadata["stored_bytes"], ref.stored_bytes)
            self.assertEqual(metadata["sha256"], ref.sha256)
            self.assertEqual(
                metadata["original_sha256"],
                hashlib.sha256(b"full output\n").hexdigest(),
            )
            self.assertEqual(ref.original_sha256, metadata["original_sha256"])

    def test_path_for_rejects_escaping_handle(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = ManagedOutputStore(td)

            with self.assertRaises(ValueError):
                store.path_for("session", "run", "../escape")

    def test_large_output_is_capped_with_head_and_tail(self) -> None:
        with tempfile.TemporaryDirectory() as td, mock.patch(
            "codey.storage.managed_outputs.MAX_MANAGED_OUTPUT_BYTES",
            40,
        ):
            store = ManagedOutputStore(td)

            ref = store.write_run_output(
                session_id="session",
                run_id="run",
                tool_id="",
                permission_profile="coding_writer",
                command="python large.py",
                cwd=".",
                text="HEAD" + ("x" * 100) + "TAIL",
            )

            self.assertIsNotNone(ref)
            assert ref is not None
            self.assertEqual(ref.original_bytes, 108)
            self.assertLessEqual(ref.stored_bytes, 40)
            self.assertTrue(ref.stored_truncated)
            stored = ref.path.read_text(encoding="utf-8")
            self.assertTrue(stored.startswith("HEAD"))
            self.assertTrue(stored.endswith("TAIL"))
            self.assertIn("[... omitted ...]", stored)

    def test_truncated_store_proves_original_bytes(self) -> None:
        full = "HEAD" + ("x" * 100) + "TAIL"
        with tempfile.TemporaryDirectory() as td, mock.patch(
            "codey.storage.managed_outputs.MAX_MANAGED_OUTPUT_BYTES",
            40,
        ):
            store = ManagedOutputStore(td)
            ref = store.write_run_output(
                session_id="session",
                run_id="run",
                tool_id="",
                permission_profile="coding_writer",
                command="python large.py",
                cwd=".",
                text=full,
            )

            assert ref is not None
            stored = ref.path.read_text(encoding="utf-8")
            self.assertTrue(ref.stored_truncated)
            self.assertEqual(ref.original_sha256, hashlib.sha256(full.encode("utf-8")).hexdigest())
            self.assertEqual(ref.sha256, hashlib.sha256(stored.encode("utf-8")).hexdigest())
            self.assertNotEqual(ref.sha256, ref.original_sha256)

            from codey.runtime.core.models import ToolCall, ToolResult

            result = ToolResult(
                call=ToolCall(name="run", args={}),
                model_text="clipped",
                truncated=True,
                audit={"managed_output": {
                    "handle": ref.handle,
                    "original_bytes": ref.original_bytes,
                    "stored_bytes": ref.stored_bytes,
                    "sha256": ref.sha256,
                    "original_sha256": ref.original_sha256,
                    "stored_truncated": True,
                }},
            )
            self.assertIn(f"original_sha256={ref.original_sha256}", result.model_text)
            self.assertIn(f"sha256={ref.sha256}", result.model_text)
            self.assertIn("output receipt retained locally", result.model_text)
            self.assertNotIn("full output retained locally", result.model_text)

    def test_per_run_handle_count_is_capped(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = ManagedOutputStore(td)
            for index in range(MAX_MANAGED_OUTPUTS_PER_RUN):
                self.assertIsNotNone(
                    store.write_run_output(
                        session_id="session",
                        run_id="run",
                        tool_id=str(index),
                        permission_profile="coding_writer",
                        command="python test.py",
                        cwd=".",
                        text=f"output {index}",
                    )
                )

            self.assertIsNone(
                store.write_run_output(
                    session_id="session",
                    run_id="run",
                    tool_id="overflow",
                    permission_profile="coding_writer",
                    command="python test.py",
                    cwd=".",
                    text="overflow",
                )
            )

    def test_output_over_policy_size_limit_is_not_retained(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = ManagedOutputStore(td)

            ref = store.write_run_output(
                session_id="session",
                run_id="run",
                tool_id="oversized",
                permission_profile="coding_writer",
                command="python huge.py",
                cwd=".",
                text="x" * (MAX_MANAGED_OUTPUT_BYTES + 1),
            )

            self.assertIsNone(ref)

    def test_missing_permission_profile_does_not_write_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = ManagedOutputStore(td)

            ref = store.write_run_output(
                session_id="session",
                run_id="run",
                tool_id="missing-profile",
                permission_profile="",
                command="python test.py",
                cwd=".",
                text="output",
            )

            self.assertIsNone(ref)

    def test_write_failure_is_fail_open(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state_home = Path(td) / "state-file"
            state_home.write_text("not a directory", encoding="utf-8")
            store = ManagedOutputStore(state_home)

            self.assertIsNone(
                store.write_run_output(
                    session_id="session",
                    run_id="run",
                    tool_id="",
                    permission_profile="coding_writer",
                    command="python test.py",
                    cwd=".",
                    text="output",
                )
            )


class ManagedRunCommandTests(unittest.TestCase):
    def test_wrapper_saves_only_when_projection_is_truncated(self) -> None:
        stdout_text = "HEAD" + ("x" * 200) + "MIDDLE_SHOULD_BE_SAVED" + ("y" * 200) + "TAIL"
        completed = CapturedProcess(
            ["python", "-m", "pytest", "tests/test_large.py"],
            1,
            stdout=stdout_text,
            stderr="",
            stdout_bytes=len(stdout_text.encode("utf-8")),
            stderr_bytes=0,
            stdout_truncated=False,
            stderr_truncated=False,
        )
        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch("codey.toolchain.runtime.RUN_OUTPUT_LIMIT", 80),
            mock.patch("codey.toolchain.runtime.cancellation.run_process", return_value=completed),
        ):
            store = ManagedOutputStore(Path(td) / "state")
            outcome = run_command_with_managed_output(
                Path(td),
                ".",
                "python -m pytest tests/test_large.py",
                permission_profile="coding_writer",
                store=store,
                session_id="session",
                run_id="run",
            )
            managed = outcome.audit["managed_output"]
            saved = store.path_for(
                "session",
                "run",
                str(managed["handle"]),
            ).read_text(
                encoding="utf-8"
            )

        self.assertTrue(outcome.truncated)
        self.assertTrue(str(managed["handle"]).startswith("out_"))
        self.assertEqual(managed["original_bytes"], managed["stored_bytes"])
        self.assertIn("MIDDLE_SHOULD_BE_SAVED", saved)
        self.assertNotIn("MIDDLE_SHOULD_BE_SAVED", outcome.model_text)

    def test_wrapper_does_not_save_short_output(self) -> None:
        completed = CapturedProcess(
            ["python", "-m", "py_compile", "ok.py"],
            0,
            stdout="OK",
            stderr="",
            stdout_bytes=2,
            stderr_bytes=0,
            stdout_truncated=False,
            stderr_truncated=False,
        )
        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch("codey.toolchain.runtime.cancellation.run_process", return_value=completed),
        ):
            store = ManagedOutputStore(Path(td) / "state")
            outcome = run_command_with_managed_output(
                Path(td),
                ".",
                "python -m py_compile ok.py",
                permission_profile="coding_writer",
                store=store,
                session_id="session",
                run_id="run",
            )

        self.assertFalse(outcome.truncated)
        self.assertEqual(outcome.managed_output(), {})


if __name__ == "__main__":
    unittest.main()
