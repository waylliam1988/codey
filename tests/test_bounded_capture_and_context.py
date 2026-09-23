"""Bounded capture + pre-send context accounting (first-round runtime fixes)."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from codey.runtime.core import cancellation
from codey.runtime.core.output_capture import BoundedByteCapture


class BoundedByteCaptureTests(unittest.TestCase):
    def test_head_tail_and_omitted_marker(self) -> None:
        capture = BoundedByteCapture(head_limit=16, tail_limit=16)
        # 8 KiB reads in production; here feed in odd splits on purpose.
        payload = b"A" * 16 + b"B" * 100 + b"C" * 16
        for index in range(0, len(payload), 7):
            capture.feed(payload[index:index + 7])
        done = capture.finish()
        self.assertTrue(done.truncated)
        self.assertEqual(done.total_bytes, len(payload))
        self.assertEqual(done.omitted_bytes, len(payload) - 32)
        self.assertIn(f"omitted {done.omitted_bytes} bytes", done.text)
        self.assertTrue(done.text.startswith("A" * 16))
        self.assertTrue(done.text.endswith("C" * 16))
        self.assertLess(len(done.text.encode("utf-8")), len(payload))

    def test_small_output_not_truncated(self) -> None:
        capture = BoundedByteCapture(head_limit=64, tail_limit=64)
        capture.feed(b"hello")
        done = capture.finish()
        self.assertFalse(done.truncated)
        self.assertEqual(done.text, "hello")
        self.assertEqual(done.total_bytes, 5)

    def test_chinese_split_across_feeds_and_edges(self) -> None:
        # "中文" * N: 3 bytes per char; splits land inside characters.
        unit = "中文".encode()
        payload = unit * 40  # 240 bytes
        capture = BoundedByteCapture(head_limit=30, tail_limit=30)
        for index in range(0, len(payload), 7):
            capture.feed(payload[index:index + 7])
        done = capture.finish()
        self.assertTrue(done.truncated)
        self.assertEqual(done.total_bytes, len(payload))
        # Edges must not surface as replacement chars from the cut itself:
        # decode the retained head/tail slices strictly after trimming.
        head_text = done.text.split("omitted")[0]
        tail_text = done.text.split("...]")[1]
        self.assertNotIn("�", head_text[-4:])
        self.assertNotIn("�", tail_text[:4])
        # Interior content round-trips.
        self.assertIn("中文", done.text)


class RunProcessBoundedTests(unittest.TestCase):
    def test_large_dual_output_stays_bounded(self) -> None:
        script = (
            "import sys; "
            "sys.stdout.write('x' * 1000000); "
            "sys.stderr.write('y' * 500000)"
        )
        completed = cancellation.run_process(
            [sys.executable, "-c", script],
            cwd=".",
            timeout=30,
            capture_limit_bytes=65536,
        )
        self.assertEqual(completed.stdout_bytes, 1000000)
        self.assertEqual(completed.stderr_bytes, 500000)
        self.assertTrue(completed.stdout_truncated)
        self.assertTrue(completed.stderr_truncated)
        self.assertLess(len(completed.stdout.encode("utf-8")), 1000000)
        self.assertIn("omitted", completed.stdout)

    def test_drain_timeout_when_grandchild_holds_pipe(self) -> None:
        block = threading.Event()

        class BlockingStream:
            def read(self, _size: int) -> bytes:
                block.wait(timeout=30)
                return b""

            def close(self) -> None:
                block.set()

        proc = SimpleNamespace(
            pid=99999999,
            returncode=0,
            stdout=BlockingStream(),
            stderr=None,
            wait=lambda timeout=None: 0,
            terminate=lambda: None,
            kill=lambda: None,
        )
        job = SimpleNamespace(close=lambda: None)
        with mock.patch(
            "codey.runtime.core.output_capture.DRAIN_TIMEOUT_SECONDS", 0.2
        ), self.assertRaises(cancellation.PipeDrainTimeout):
            cancellation.wait_process(
                proc, job, ["cmd"], 30, capture_limit_bytes=65536
            )
        block.set()

    def test_timeout_and_stop(self) -> None:
        with self.assertRaises(subprocess.TimeoutExpired):
            cancellation.run_process(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                cwd=".",
                timeout=0.5,
                capture_limit_bytes=65536,
            )
        event = threading.Event()
        event.set()
        with cancellation.scope(event), self.assertRaises(cancellation.TaskCancelled):
            cancellation.run_process(
                [sys.executable, "-c", "print('hi')"],
                cwd=".",
                timeout=30,
                capture_limit_bytes=65536,
            )


class TruncationPropagationTests(unittest.TestCase):
    def test_tool_result_marks_capture_truncation(self) -> None:
        from codey.toolchain.runtime import (
            RunCommandRawResult,
            project_run_command_result,
        )

        with tempfile.TemporaryDirectory() as td:
            raw = RunCommandRawResult(
                command="pytest -q",
                output="x" * 100,
                ok=True,
                exit_code=0,
                stdout_bytes=300000,
                stderr_bytes=0,
                capture_truncated=True,
            )
            outcome = project_run_command_result(Path(td), raw)
        self.assertTrue(outcome.truncated)
        self.assertTrue(outcome.audit.get("capture_truncated"))
        self.assertEqual(outcome.audit.get("process_bytes"), 300000)
        self.assertIn("narrower command", outcome.model_text)

    def test_shell_result_merges_capture_truncation(self) -> None:
        from codey.app import shell_service
        from codey.runtime.core.cancellation import CapturedProcess

        completed = CapturedProcess(
            args="cmd",
            returncode=0,
            stdout="HEAD",
            stderr="",
            stdout_bytes=300000,
            stderr_bytes=0,
            stdout_truncated=True,
            stderr_truncated=False,
        )
        with tempfile.TemporaryDirectory() as td:
            ctx = SimpleNamespace(
                approval_generation=lambda: 0,
                run_registry=SimpleNamespace(stop_flag=threading.Event()),
                lock=threading.Lock(),
            )
            ctx._shell_spawn_gate = threading.Lock()
            with (
                mock.patch.object(
                    cancellation, "start_process",
                    return_value=(mock.Mock(), mock.Mock()),
                ),
                mock.patch.object(
                    cancellation, "wait_process", return_value=completed
                ),
            ):
                data = shell_service.execute_approved_shell(ctx, td, ".", "cmd")
        self.assertTrue(data["truncated"])
        self.assertTrue(data.get("capture_truncated"))

    def test_managed_output_distinguishes_actual_vs_stored(self) -> None:
        from codey.storage.managed_outputs import (
            ManagedOutputStore,
            run_command_with_managed_output,
        )
        from codey.toolchain.runtime import RunCommandRawResult

        raw = RunCommandRawResult(
            command="pytest -q",
            output="HEAD" + "x" * 50000 + "TAIL",
            ok=True,
            exit_code=0,
            stdout_bytes=1000000,
            stderr_bytes=0,
            capture_truncated=True,
        )
        with tempfile.TemporaryDirectory() as td:
            store = ManagedOutputStore(Path(td) / "state")
            with mock.patch(
                "codey.storage.managed_outputs.tool_runtime.run_command_raw",
                return_value=raw,
            ):
                outcome = run_command_with_managed_output(
                    Path(td), ".", "pytest -q",
                    permission_profile="coding_writer",
                    store=store,
                    session_id="s",
                    run_id="r",
                )
        managed = outcome.audit["managed_output"]
        self.assertTrue(outcome.truncated)
        self.assertEqual(managed["original_bytes"], 1000000)
        self.assertLess(managed["stored_bytes"], 1000000)
        self.assertTrue(managed["stored_truncated"])
        self.assertTrue(outcome.audit.get("capture_truncated"))

    def test_worker_truncated_output_is_not_parsed(self) -> None:
        from codey.repairs import self_repair_worker as worker
        from codey.repairs.self_repair import SelfRepairJob
        from codey.runtime.core.cancellation import CapturedProcess

        job = SelfRepairJob(
            provider_id="deepseek", failure_kind="k", failure_stage="s",
            failure_facts={},
        )
        completed = CapturedProcess(
            args="worker",
            returncode=0,
            stdout='{"ok": true,',
            stderr="",
            stdout_bytes=99999,
            stderr_bytes=0,
            stdout_truncated=True,
            stderr_truncated=False,
        )
        with mock.patch.object(
            worker.cancellation, "run_process", return_value=completed
        ):
            result = worker.run_self_repair_worker(
                job, helper_ids=(), state_home=".", source_root=".",
            )
        self.assertFalse(result.ok)
        self.assertIn("输出超限", result.error)


class LocalPrepareRequestTests(unittest.TestCase):
    def _provider(self):
        from codey.providers.local_openai import LocalOpenAIProvider

        return LocalOpenAIProvider(
            base_url="http://127.0.0.1:9/v1",
            model="test",
            context_window_tokens=200,
            context_reserve_tokens=50,
            context_keep_recent_tokens=60,
        )

    def test_history_ok_but_pending_overflows_with_zero_http_calls(self) -> None:
        from codey.providers import error_classification as errors

        provider = self._provider()
        provider._messages = [
            {"role": "user", "content": "short history"}
        ]
        before = [dict(message) for message in provider._messages]
        with mock.patch.object(
            provider, "_post_chat",
            side_effect=AssertionError("must not send"),
        ), self.assertRaises(errors.ContextOverflowError):
            provider.send("x" * 5000)
        self.assertEqual(provider._messages, before)

    def test_tools_and_tool_results_overflow(self) -> None:
        from codey.providers import error_classification as errors

        provider = self._provider()
        provider._messages.append({
            "role": "assistant", "content": "",
            "tool_calls": [{"id": "call_1"}],
        })
        before = [dict(message) for message in provider._messages]
        tools = [{
            "type": "function",
            "function": {"name": "read", "description": "x" * 3000},
        }]
        results = [
            {"tool_call_id": "call_1", "content": "y" * 3000},
            {"tool_call_id": "call_1", "content": "z" * 3000},
        ]
        with mock.patch.object(
            provider, "_post_chat",
            side_effect=AssertionError("must not send"),
        ), self.assertRaises(errors.ContextOverflowError):
            provider.send_tool_results(results, tools)
        self.assertEqual(provider._messages, before)

    def test_single_huge_input_overflows(self) -> None:
        from codey.providers import error_classification as errors

        provider = self._provider()
        before: list[dict] = []
        with mock.patch.object(
            provider, "_post_chat",
            side_effect=AssertionError("must not send"),
        ), self.assertRaises(errors.ContextOverflowError):
            provider.send("z" * 20000)
        self.assertEqual(provider._messages, before)

    def test_compaction_failure_is_explicit(self) -> None:
        provider = self._provider()
        before: list[dict] = []
        with (
            mock.patch(
                "codey.agents.context_compaction.compact_openai_messages_in_place",
                side_effect=RuntimeError("boom"),
            ),
            mock.patch.object(
                provider, "_post_chat",
                side_effect=AssertionError("must not send"),
            ),self.assertRaisesRegex(RuntimeError, "compaction failed")
        ):
            provider.send("hello")
        self.assertEqual(provider._messages, before)


if __name__ == "__main__":
    unittest.main()
