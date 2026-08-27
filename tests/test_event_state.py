"""Safe event-backed state reset test suite."""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from codey.storage.event_state import reset_event_backed_state
from codey.storage.file_lock import with_file_lock
from codey.ghost.work_queue import GhostWorkQueueStore


class ResetEventBackedStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_safely_deletes_all_specified_paths(self) -> None:
        events = self.root / "store_events.jsonl"
        proj1 = self.root / "store_projection.json"
        proj2 = self.root / "store_settings.json"

        events.write_text("event1\n", encoding="utf-8")
        proj1.write_text("{}", encoding="utf-8")
        proj2.write_text("{}", encoding="utf-8")

        self.assertTrue(events.exists())
        self.assertTrue(proj1.exists())
        self.assertTrue(proj2.exists())

        reset_event_backed_state(events, proj1, proj2)

        self.assertFalse(events.exists())
        self.assertFalse(proj1.exists())
        self.assertFalse(proj2.exists())

    def test_blocks_when_events_are_locked_by_another_thread(self) -> None:
        events = self.root / "events.jsonl"
        proj = self.root / "proj.json"
        events.write_text("evt\n", encoding="utf-8")
        proj.write_text("{}", encoding="utf-8")

        started = threading.Event()
        release_holder = threading.Event()
        holder_finished = False

        def holder() -> None:
            nonlocal holder_finished
            with with_file_lock(events, timeout_seconds=5.0):
                started.set()
                release_holder.wait(timeout=2.0)
            holder_finished = True

        t = threading.Thread(target=holder)
        t.start()
        started.wait(timeout=2.0)

        reset_done = False

        def run_reset() -> None:
            nonlocal reset_done
            reset_event_backed_state(events, proj)
            reset_done = True

        rt = threading.Thread(target=run_reset)
        rt.start()

        # Give reset thread a moment to try acquiring and block
        time.sleep(0.1)
        self.assertFalse(reset_done)
        self.assertTrue(events.exists())

        # Unblock holder
        release_holder.set()
        t.join()
        rt.join()

        self.assertTrue(reset_done)
        self.assertFalse(events.exists())
        self.assertFalse(proj.exists())

    def test_store_reset_all_returns_false_on_lock_timeout(self) -> None:
        store = GhostWorkQueueStore(state_home=self.root)
        store.events_path.parent.mkdir(parents=True, exist_ok=True)
        store.events_path.write_text("event\n", encoding="utf-8")

        started = threading.Event()
        stop = threading.Event()

        def lock_holder() -> None:
            with with_file_lock(store.events_path, timeout_seconds=5.0):
                started.set()
                stop.wait(timeout=3.0)

        t = threading.Thread(target=lock_holder)
        t.start()
        started.wait(timeout=2.0)
        try:
            # When events_path is locked, reset_all attempting to acquire with short timeout will fail gracefully
            import codey.storage.file_lock as fl
            orig_timeout = fl.LOCK_TIMEOUT_SECONDS
            fl.LOCK_TIMEOUT_SECONDS = 0.1
            try:
                result = store.reset_all()
                self.assertFalse(result)
            finally:
                fl.LOCK_TIMEOUT_SECONDS = orig_timeout
        finally:
            stop.set()
            t.join()


if __name__ == "__main__":
    unittest.main()
