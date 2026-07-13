from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from codey import cancellation


class CancellationTests(unittest.TestCase):
    def test_set_event_interrupts_shared_wait(self) -> None:
        event = threading.Event()
        event.set()

        with cancellation.scope(event):
            with self.assertRaises(cancellation.TaskCancelled):
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
            mock.patch.object(cancellation.time, "sleep") as sleep,
        ):
            with cancellation.deadline_scope(10.0):
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
        with cancellation.scope(event), cancellation.deadline_scope(0.0):
            with self.assertRaises(cancellation.TaskCancelled):
                cancellation.check()

    def test_deadline_uses_process_tree_cleanup(self) -> None:
        proc = mock.Mock()
        job = mock.Mock()
        job_type = mock.Mock(return_value=job)

        with (
            mock.patch.object(cancellation.subprocess, "Popen", return_value=proc),
            mock.patch.object(cancellation, "_WindowsJob", job_type),
            mock.patch.object(
                cancellation,
                "check",
                side_effect=[None, cancellation.DeadlineExceeded("timed out")],
            ),
            mock.patch.object(cancellation, "_terminate_process_tree") as terminate,
        ):
            with self.assertRaises(cancellation.DeadlineExceeded):
                cancellation.run_process(
                    [sys.executable, "worker.py"],
                    cwd=".",
                    timeout=30,
                )

        expected_job = job if os.name == "nt" else None
        terminate.assert_called_once_with(proc, expected_job)

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
                cancellation.deadline_scope(time.monotonic() + 1.0),
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
                )

            self.assertLess(time.monotonic() - started, 2.0)
            self.assertTrue(parent_pid.exists())
            self.assertTrue(child_pid.exists())
            self.assertFalse(_windows_process_is_active(int(parent_pid.read_text())))
            self.assertFalse(_windows_process_is_active(int(child_pid.read_text())))

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

            def stop_after_child_starts() -> None:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not child_pid.exists():
                    time.sleep(0.01)
                event.set()

            stopper = threading.Thread(target=stop_after_child_starts)
            started = time.monotonic()
            stopper.start()
            try:
                with cancellation.scope(event):
                    with self.assertRaises(cancellation.TaskCancelled):
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
                        )
            finally:
                stopper.join(timeout=5)

            self.assertLess(time.monotonic() - started, 1.0)
            self.assertTrue(parent_pid.exists())
            self.assertTrue(child_pid.exists())
            self.assertFalse(_windows_process_is_active(int(parent_pid.read_text())))
            self.assertFalse(_windows_process_is_active(int(child_pid.read_text())))


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
