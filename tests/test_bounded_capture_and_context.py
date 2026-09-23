"""Bounded capture + pre-send context accounting (first-round runtime fixes)."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from codey.runtime.core import cancellation
from codey.runtime.core.output_capture import BoundedByteCapture


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, wintypes.LPDWORD]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


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

    def test_omitted_bytes_recomputed_after_utf8_trim(self) -> None:
        # Non-aligned cut: head/tail limits split multi-byte characters,
        # so the marker must count trimmed edge bytes as omitted too.
        payload = ("中" * 60).encode("utf-8")  # 180 bytes
        capture = BoundedByteCapture(head_limit=10, tail_limit=10)
        capture.feed(payload)
        done = capture.finish()
        self.assertTrue(done.truncated)
        head_text = done.text.split("\n[...")[0]
        tail_text = done.text.split("...]\n")[1]
        recomputed = (
            done.total_bytes
            - len(head_text.encode("utf-8"))
            - len(tail_text.encode("utf-8"))
        )
        self.assertEqual(done.omitted_bytes, recomputed)
        self.assertIn(f"omitted {recomputed} bytes", done.text)


class ReaderErrorTests(unittest.TestCase):
    def _error_proc(self, stream: object) -> SimpleNamespace:
        killed: list[bool] = []

        return SimpleNamespace(
            pid=99999999,
            returncode=0,
            stdout=stream,
            stderr=None,
            wait=lambda timeout=None: 0,
            terminate=lambda: killed.append(True),
            kill=lambda: killed.append(True),
            _killed=killed,
        )

    class _FailAfterPartial:
        def __init__(self, chunks: list[bytes]) -> None:
            self._chunks = list(chunks)

        def read(self, _size: int) -> bytes:
            if not self._chunks:
                raise OSError("simulated pipe read failure")
            return self._chunks.pop(0)

        def close(self) -> None:
            pass

    def test_stdout_read_error_after_partial_data_is_not_success(self) -> None:
        proc = self._error_proc(self._FailAfterPartial([b"partial-", b"data"]))
        job = SimpleNamespace(close=lambda: None)
        with self.assertRaises(cancellation.ProcessOutputReadError):
            cancellation.wait_process(
                proc, job, ["cmd"], 30, capture_limit_bytes=65536
            )
        self.assertTrue(proc._killed)

    def test_stderr_read_error_is_not_success(self) -> None:
        proc = self._error_proc(None)
        proc.stderr = self._FailAfterPartial([b"oops"])
        job = SimpleNamespace(close=lambda: None)
        with self.assertRaises(cancellation.ProcessOutputReadError):
            cancellation.wait_process(
                proc, job, ["cmd"], 30, capture_limit_bytes=65536
            )
        self.assertTrue(proc._killed)

    def test_run_command_maps_read_error_to_failure(self) -> None:
        from codey.toolchain import runtime as tool_runtime

        with tempfile.TemporaryDirectory() as td, mock.patch.object(
            tool_runtime.cancellation,
            "run_process",
            side_effect=cancellation.ProcessOutputReadError("boom"),
        ):
            outcome = tool_runtime.run_command(
                Path(td), ".", "python -m pytest tests/test_x.py",
                permission_profile="coding_writer",
            )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_code, "output_read_error")
        self.assertIn("failed reading command output", outcome.model_text)

    def test_shell_maps_read_error_to_failure(self) -> None:
        from codey.app import shell_service

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
                    cancellation, "wait_process",
                    side_effect=cancellation.ProcessOutputReadError("boom"),
                ),
            ):
                data = shell_service.execute_approved_shell(ctx, td, ".", "cmd")
        self.assertFalse(data["ok"])
        self.assertEqual(data["status"], "output_read_error")

    def test_worker_maps_read_error_distinctly(self) -> None:
        from codey.repairs import self_repair_worker as worker
        from codey.repairs.self_repair import SelfRepairJob

        job = SelfRepairJob(
            provider_id="deepseek", failure_kind="k", failure_stage="s",
            failure_facts={},
        )
        with mock.patch.object(
            worker.cancellation,
            "run_process",
            side_effect=cancellation.ProcessOutputReadError("boom"),
        ):
            result = worker.run_self_repair_worker(
                job, helper_ids=(), state_home=".", source_root=".",
            )
        self.assertFalse(result.ok)
        self.assertIn("failed reading worker output", result.error)

    def test_worker_maps_drain_timeout_distinctly(self) -> None:
        from codey.repairs import self_repair_worker as worker
        from codey.repairs.self_repair import SelfRepairJob

        job = SelfRepairJob(
            provider_id="deepseek", failure_kind="k", failure_stage="s",
            failure_facts={},
        )
        with mock.patch.object(
            worker.cancellation,
            "run_process",
            side_effect=cancellation.PipeDrainTimeout("no eof"),
        ):
            result = worker.run_self_repair_worker(
                job, helper_ids=(), state_home=".", source_root=".",
            )
        self.assertFalse(result.ok)
        self.assertIn("pipe drain timeout", result.error)


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

    def test_real_grandchild_holding_pipe_drains_then_cleans_tree(self) -> None:
        # A parent that spawns a pipe-inheriting grandchild and exits:
        # the drain must time out (bounded) and the grandchild must be gone.
        with tempfile.TemporaryDirectory() as td:
            pid_file = Path(td) / "grandchild.pid"
            parent = Path(td) / "parent.py"
            parent.write_text(
                "import subprocess, sys\n"
                "child = subprocess.Popen("
                "[sys.executable, '-c', 'import time; time.sleep(30)'])\n"
                f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n",
                encoding="utf-8",
            )
            started = time.monotonic()
            with self.assertRaises(cancellation.PipeDrainTimeout):
                cancellation.run_process(
                    [sys.executable, str(parent)],
                    cwd=td,
                    timeout=30,
                    capture_limit_bytes=65536,
                )
            elapsed = time.monotonic() - started
            # Drain (2s) + reader join (2s) bound the cleanup; a blocking
            # close() would blow this budget.
            self.assertLess(elapsed, 15.0)
            grandchild_pid = int(pid_file.read_text(encoding="utf-8").strip())
            deadline = time.monotonic() + 10.0
            while _pid_alive(grandchild_pid) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(
                _pid_alive(grandchild_pid),
                "grandchild holding the pipe survived the tree cleanup",
            )

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
                started_at="2026-09-24T00:00:00.000Z",
                finished_at="2026-09-24T00:00:01.000Z",
                duration_ms=100,
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

    def test_managed_output_matches_stored_json(self) -> None:
        import json as _json

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
            started_at="2026-09-24T00:00:00.000Z",
            finished_at="2026-09-24T00:00:01.000Z",
            duration_ms=100,
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
                # Read the on-disk metadata before the directory exits.
                managed = outcome.audit["managed_output"]
                stored = _json.loads(
                    store.metadata_path_for(
                        "s", "r", str(managed["handle"])
                    ).read_text(encoding="utf-8")
                )
            self.assertTrue(outcome.truncated)
            for key in (
                "handle", "original_bytes", "stored_bytes",
                "sha256", "original_sha256", "stored_truncated",
            ):
                self.assertEqual(managed[key], stored[key])
            self.assertEqual(outcome.audit.get("process_bytes"), 1000000)
            self.assertTrue(outcome.audit.get("capture_truncated"))
            self.assertIn("output receipt retained locally", outcome.model_text)

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

    def test_analysis_run_counts_capture_truncation(self) -> None:
        from codey.research import analysis_run

        record = analysis_run.analysis_run_record({
            "run_id": "run-1",
            "tool_id": "1:0",
            "tool_name": "run",
            "command": "pytest -q",
            "cwd": ".",
            "project": "proj",
            "exit_code": 0,
            "ok": True,
            "started_at": "2026-09-24T00:00:00Z",
            "finished_at": "2026-09-24T00:00:01Z",
            "duration_ms": 100,
            "managed_output": {
                "handle": "out_0001_abc123def456",
                "original_bytes": 50008,
                "stored_bytes": 50008,
                "sha256": "b" * 64,
                "original_sha256": "b" * 64,
                "stored_truncated": False,
            },
            "capture_truncated": True,
        })
        self.assertIsNotNone(record)
        assert record is not None
        self.assertTrue(record.stored_truncated)
        self.assertIn("capture_truncated", record.warnings)


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
            "tool_calls": [{"id": "call_1"}, {"id": "call_2"}],
        })
        before = [dict(message) for message in provider._messages]
        tools = [{
            "type": "function",
            "function": {"name": "read", "description": "x" * 3000},
        }]
        results = [
            {"tool_call_id": "call_1", "content": "y" * 3000},
            {"tool_call_id": "call_2", "content": "z" * 3000},
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
        from codey.providers import error_classification as errors

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
            ),self.assertRaisesRegex(errors.RequestPrepError, "compaction failed")
        ):
            provider.send("hello")
        self.assertEqual(provider._messages, before)

    def test_prep_failure_settles_not_sent_without_rollover(self) -> None:
        from codey.agents import prompt_context
        from codey.providers import error_classification as errors
        from codey.runtime.effects.effect_records import SENT_STATE_NOT_SENT

        exc = errors.RequestPrepError("local context compaction failed: boom")
        # A prep failure is not a context overflow: no rollover retry.
        self.assertNotIsInstance(exc, errors.ContextOverflowError)
        mutations = mock.Mock()
        session = SimpleNamespace(session_id="s", run_id="r")
        settled = prompt_context._fail_provider_send(
            session, mutations, "eff-1", exc
        )
        self.assertTrue(settled)
        settlement = mutations.settle_provider_effect.call_args.args[2]
        self.assertEqual(settlement.sent_state, SENT_STATE_NOT_SENT)


if __name__ == "__main__":
    unittest.main()
