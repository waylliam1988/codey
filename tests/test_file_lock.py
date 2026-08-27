"""OS-backed advisory file locks test suite."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from codey.storage.file_lock import (
    LockTimeout,
    with_file_lock,
)


class FileLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_lock_timeout_inheritance(self) -> None:
        self.assertTrue(issubclass(LockTimeout, TimeoutError))
        self.assertTrue(issubclass(LockTimeout, OSError))

    def test_os_error_catch_handles_lock_timeout(self) -> None:
        def safe_operation() -> bool:
            try:
                raise LockTimeout("timed out")
            except OSError:
                return False

        self.assertFalse(safe_operation())

    def test_lock_is_reentrant_within_thread(self) -> None:
        target = self.root / "test_state.json"
        with with_file_lock(target, timeout_seconds=2.0):
            with with_file_lock(target, timeout_seconds=2.0):
                with with_file_lock(target, timeout_seconds=2.0):
                    pass

    def test_threads_mutually_exclude(self) -> None:
        target = self.root / "shared_file.json"
        active_holders = 0
        max_concurrent = 0
        lock = threading.Lock()

        def worker() -> None:
            nonlocal active_holders, max_concurrent
            with with_file_lock(target, timeout_seconds=5.0):
                with lock:
                    active_holders += 1
                    if active_holders > max_concurrent:
                        max_concurrent = active_holders
                time.sleep(0.05)
                with lock:
                    active_holders -= 1

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(max_concurrent, 1)

    def test_timeout_raises_locktimeout_when_held_by_another_thread(self) -> None:
        target = self.root / "state.json"
        started = threading.Event()
        stop = threading.Event()

        def holder() -> None:
            with with_file_lock(target, timeout_seconds=5.0):
                started.set()
                stop.wait(timeout=3.0)

        t = threading.Thread(target=holder)
        t.start()
        started.wait(timeout=2.0)
        try:
            with self.assertRaises(LockTimeout):
                with with_file_lock(target, timeout_seconds=0.2):
                    pass
        finally:
            stop.set()
            t.join()

    def test_lock_file_remains_on_disk_after_release(self) -> None:
        target = self.root / "data.json"
        lock_path = target.with_name(f".{target.name}.lock")
        self.assertFalse(lock_path.exists())

        with with_file_lock(target):
            self.assertTrue(lock_path.exists())

        # The lock file is a permanent advisory lock carrier and must NOT be deleted.
        self.assertTrue(lock_path.exists())

    def test_cross_process_mutual_exclusion(self) -> None:
        target = self.root / "cross_proc.json"
        script = f"""
import sys, time
from codey.storage.file_lock import with_file_lock

with with_file_lock({repr(str(target))}, timeout_seconds=5.0):
    sys.stdout.write("ACQUIRED\\n")
    sys.stdout.flush()
    time.sleep(1.0)
"""
        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        try:
            line = proc.stdout.readline()
            self.assertEqual(line.strip(), "ACQUIRED")

            # Main process should time out trying to acquire while child holds it
            with self.assertRaises(LockTimeout):
                with with_file_lock(target, timeout_seconds=0.2):
                    pass

            proc.wait(timeout=3.0)
            self.assertEqual(proc.returncode, 0)

            # After child exits, main process should succeed immediately
            with with_file_lock(target, timeout_seconds=1.0):
                pass
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


if __name__ == "__main__":
    unittest.main()
