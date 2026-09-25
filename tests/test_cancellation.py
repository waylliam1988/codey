from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from codey.runtime.core import cancellation


class CancellationTests(unittest.TestCase):
    def test_set_event_interrupts_shared_wait(self) -> None:
        event = threading.Event()
        event.set()

        with cancellation.scope(event), self.assertRaises(cancellation.TaskCancelled):
            cancellation.wait(30)

    def test_scope_restores_previous_event(self) -> None:
        outer = threading.Event()
        inner = threading.Event()

        with cancellation.scope(outer):
            self.assertIs(cancellation.current_event(), outer)
            with cancellation.scope(inner):
                self.assertIs(cancellation.current_event(), inner)
            self.assertIs(cancellation.current_event(), outer)

    def test_deadline_caps_wait_and_restores_previous_scope(self) -> None:
        with (
            mock.patch.object(
                cancellation.time,
                "monotonic",
                side_effect=[9.0, 9.0, 10.0],
            ),
            mock.patch.object(cancellation.time, "sleep") as sleep,cancellation.deadline_scope(10.0)
        ):
            self.assertEqual(cancellation.current_deadline(), 10.0)
            with self.assertRaises(cancellation.DeadlineExceeded):
                cancellation.wait(30)

        sleep.assert_called_once_with(1.0)
        self.assertIsNone(cancellation.current_deadline())

    def test_nested_deadline_scope_keeps_earliest_deadline(self) -> None:
        with cancellation.deadline_scope(10.0):
            with cancellation.deadline_scope(20.0):
                self.assertEqual(cancellation.current_deadline(), 10.0)
            self.assertEqual(cancellation.current_deadline(), 10.0)

    def test_user_cancellation_wins_over_expired_deadline(self) -> None:
        event = threading.Event()
        event.set()
        with cancellation.scope(event), cancellation.deadline_scope(0.0), self.assertRaises(cancellation.TaskCancelled):
            cancellation.check()

    def test_deadline_uses_process_tree_cleanup(self) -> None:
        import subprocess as _subprocess

        proc = mock.Mock()
        proc.stdout = None
        proc.stderr = None
        proc.wait.side_effect = _subprocess.TimeoutExpired("worker.py", 30)
        job = mock.Mock()

        with (
            mock.patch.object(
                cancellation,
                "check",
                side_effect=[None, cancellation.DeadlineExceeded("timed out")],
            ),
            mock.patch.object(cancellation, "_terminate_process_tree") as terminate,
            self.assertRaises(cancellation.DeadlineExceeded),
        ):
            cancellation.wait_process(
                proc,
                job,
                [sys.executable, "worker.py"],
                30,
                capture_limit_bytes=65536,
            )

        terminate.assert_called_once_with(proc, job)

    @unittest.skipUnless(os.name == "nt", "Windows Job Object regression")
    def test_deadline_terminates_real_parent_and_child_processes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            parent_pid = root / "parent.pid"
            child_pid = root / "child.pid"
            child = root / "child.py"
            parent = root / "parent.py"
            child.write_text(
                "import os, pathlib, sys, time\n"
                "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
                "time.sleep(30)\n",
                encoding="utf-8",
            )
            parent.write_text(
                "import os, pathlib, subprocess, sys, time\n"
                "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
                "subprocess.Popen([sys.executable, sys.argv[2], sys.argv[3]])\n"
                "time.sleep(30)\n",
                encoding="utf-8",
            )
            started = time.monotonic()

            with (
                cancellation.deadline_scope(time.monotonic() + 2.0),
                self.assertRaises(cancellation.DeadlineExceeded),
            ):
                cancellation.run_process(
                    [
                        sys.executable,
                        str(parent),
                        str(parent_pid),
                        str(child),
                        str(child_pid),
                    ],
                    cwd=root,
                    timeout=30,
                    capture_limit_bytes=65536,
                )

            self.assertLess(time.monotonic() - started, 8.0)
            self.assertTrue(parent_pid.exists())
            self.assertTrue(child_pid.exists())
            parent_raw = parent_pid.read_text(encoding="utf-8").strip()
            child_raw = child_pid.read_text(encoding="utf-8").strip()
            if parent_raw:
                self.assertFalse(_windows_process_is_active(int(parent_raw)))
            if child_raw:
                self.assertFalse(_windows_process_is_active(int(child_raw)))

    @unittest.skipUnless(os.name == "nt", "Windows Job Object regression")
    def test_attach_failure_kills_parent_and_closes_pipes(self) -> None:
        created: list = []
        real_popen = cancellation.subprocess.Popen

        def _spy(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            created.append(proc)
            return proc

        with (
            mock.patch.object(cancellation.subprocess, "Popen", side_effect=_spy),
            mock.patch.object(
                cancellation, "_WindowsJob", side_effect=OSError("job boom")
            ),
            self.assertRaises(OSError),
        ):
            cancellation.start_process(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                cwd=".",
            )
        self.assertEqual(len(created), 1)
        proc = created[0]
        self.assertIsNotNone(proc.poll())
        self.assertTrue(proc.stdout.closed)
        self.assertTrue(proc.stderr.closed)

    @unittest.skipUnless(os.name == "nt", "Windows Job Object regression")
    def test_cancel_terminates_real_parent_and_child_processes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            parent_pid = root / "parent.pid"
            child_pid = root / "child.pid"
            child = root / "child.py"
            parent = root / "parent.py"
            child.write_text(
                "import os, pathlib, sys, time\n"
                "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
                "time.sleep(30)\n",
                encoding="utf-8",
            )
            parent.write_text(
                "import os, pathlib, subprocess, sys, time\n"
                "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
                "subprocess.Popen([sys.executable, sys.argv[2], sys.argv[3]])\n"
                "time.sleep(30)\n",
                encoding="utf-8",
            )
            event = threading.Event()
            state: dict[str, object] = {
                "ready_ok": False,
                "ready_seconds": -1.0,
                "stop_issued_at": -1.0,
            }

            def stop_after_child_starts() -> None:
                ready_started = time.monotonic()
                deadline = ready_started + 15
                while time.monotonic() < deadline and not child_pid.exists():
                    time.sleep(0.01)
                state["ready_seconds"] = time.monotonic() - ready_started
                state["ready_ok"] = child_pid.exists()
                state["stop_issued_at"] = time.monotonic()
                event.set()

            stopper = threading.Thread(target=stop_after_child_starts)
            stopper.start()
            run_ended_at = -1.0
            try:
                with cancellation.scope(event), self.assertRaises(cancellation.TaskCancelled):
                    cancellation.run_process(
                        [
                            sys.executable,
                            str(parent),
                            str(parent_pid),
                            str(child),
                            str(child_pid),
                        ],
                        cwd=root,
                        timeout=30,
                        capture_limit_bytes=65536,
                    )
            finally:
                run_ended_at = time.monotonic()
                stopper.join(timeout=15)

            ready_ok = bool(state["ready_ok"])
            ready_seconds = float(state["ready_seconds"])
            stop_issued_at = float(state["stop_issued_at"])
            self.assertTrue(
                ready_ok,
                f"child never became ready in {ready_seconds:.2f}s "
                f"(parent_pid={parent_pid.exists()}, child_pid={child_pid.exists()})",
            )
            self.assertLess(
                ready_seconds,
                15.0,
                f"readiness took {ready_seconds:.2f}s",
            )
            cleanup_seconds = run_ended_at - stop_issued_at
            self.assertLess(
                cleanup_seconds,
                10.0,
                f"cleanup after Stop took {cleanup_seconds:.2f}s "
                f"(ready took {ready_seconds:.2f}s)",
            )
            self.assertTrue(parent_pid.exists())
            self.assertTrue(child_pid.exists())
            self.assertFalse(_windows_process_is_active(int(parent_pid.read_text())))
            self.assertFalse(_windows_process_is_active(int(child_pid.read_text())))



class PipeCleanupDiagnosisTests(unittest.TestCase):
    def test_close_owned_pipes_reports_abandoned_live_readers(self) -> None:
        import io as _io

        proc = mock.Mock()
        live_stream = _io.BytesIO(b"")
        dead_stream = _io.BytesIO(b"")
        live_thread = mock.Mock()
        live_thread.is_alive.return_value = True
        dead_thread = mock.Mock()
        dead_thread.is_alive.return_value = False
        proc.stdout = live_stream
        proc.stderr = dead_stream
        owned = [(live_stream, live_thread), (dead_stream, dead_thread)]
        abandoned = cancellation._close_owned_pipes(proc, owned)
        self.assertEqual(abandoned, 1)
        # Dead pipe closed, live pipe left open for its owner.
        self.assertTrue(dead_stream.closed)
        self.assertFalse(live_stream.closed)

    def test_repeated_external_holders_accumulate_bounded_daemons(self) -> None:
        import contextlib as _contextlib
        import os as _os

        abandoned_total = 0
        live_pairs: list[tuple[object, object]] = []
        for _ in range(3):
            read_fd, write_fd = _os.pipe()
            read_file = _os.fdopen(read_fd, "rb", buffering=0)
            # External holder keeps the write end open: the reader blocks.
            started = threading.Event()

            def _block(stream=read_file, started=started) -> None:
                started.set()
                with _contextlib.suppress(Exception):
                    stream.read(1)

            thread = threading.Thread(target=_block, daemon=True)
            thread.start()
            self.assertTrue(started.wait(timeout=5.0))
            proc = mock.Mock()
            proc.stdout = read_file
            proc.stderr = None
            proc.args = ["external-holder"]
            proc.pid = 12345
            abandoned = cancellation._close_owned_pipes(proc, [(read_file, thread)])
            abandoned_total += abandoned
            live_pairs.append((read_file, thread, write_fd))
        # Each external holder abandons exactly one daemon reader by design;
        # the diagnosis must stay visible instead of claiming full recovery.
        self.assertEqual(abandoned_total, 3)
        for read_file, thread, write_fd in live_pairs:
            self.assertTrue(thread.is_alive())
            self.assertTrue(thread.daemon)
            # Releasing the external holder lets the daemon drain and exit.
            with _contextlib.suppress(OSError):
                _os.close(write_fd)
            thread.join(timeout=5.0)
            with _contextlib.suppress(Exception):
                read_file.close()

    @unittest.skipIf(os.name == "nt", "POSIX process-group contract")
    def test_terminate_process_tree_uses_group_contract(self) -> None:
        proc = mock.Mock()
        proc.pid = 424242
        proc.terminate = mock.Mock()
        proc.wait = mock.Mock()
        proc.poll = mock.Mock(return_value=0)
        job = None
        with (
            mock.patch.object(cancellation.os, "killpg", create=True) as killpg,
            mock.patch.object(cancellation, "_process_group_exists", return_value=False),
        ):
            cancellation._terminate_process_tree(proc, job)
            killpg.assert_called_once()
            args, _ = killpg.call_args
            self.assertEqual(args[0], 424242)

    def test_terminate_direct_child_does_not_signal_group(self) -> None:
        proc = mock.Mock()
        proc.poll.return_value = 0
        if os.name == "nt":
            cancellation.terminate_direct_child(proc)
            proc.terminate.assert_called_once()
            return
        with mock.patch.object(cancellation.os, "killpg", create=True) as killpg:
            cancellation.terminate_direct_child(proc)
            killpg.assert_not_called()
        proc.terminate.assert_called_once()


def _windows_process_is_active(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, wintypes.LPDWORD]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(
        process_query_limited_information,
        False,
        pid,
    )
    if not handle:
        return False
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


if __name__ == "__main__":
    unittest.main()
