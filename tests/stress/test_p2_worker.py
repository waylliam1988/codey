"""P2: browser worker crash -- generation isolation.

A timed-out (abandoned) job's late result must never surface to a later
generation: ``call()`` with a deadline raises, the stale slot is discarded,
and the next call gets exactly its own result.
"""

from __future__ import annotations

import threading
import unittest

from codey.automation.browser_worker import BrowserWorker


class WorkerGenerationTests(unittest.TestCase):
    def test_timed_out_call_abandons_stale_result(self) -> None:
        worker = BrowserWorker(name="stress-worker-abandon", max_queue_size=8)
        gate = threading.Event()

        def _hang_forever():
            gate.wait(timeout=30)
            return "A-stale"

        with self.assertRaises(TimeoutError):
            worker.call(_hang_forever, timeout=0.3)
        gate.set()
        self.assertEqual(worker.call(lambda: "B-fresh", timeout=5), "B-fresh")
        snapshot = worker.health_snapshot()
        self.assertGreaterEqual(snapshot.cancelled_jobs, 1)

    def test_stale_completion_never_pollutes_next_generation(self) -> None:
        worker = BrowserWorker(name="stress-worker-generation", max_queue_size=8)
        release = threading.Event()
        slow_started = threading.Event()

        def _slow():
            slow_started.set()
            release.wait(timeout=30)
            return "stale-A"

        with self.assertRaises(TimeoutError):
            worker.call(_slow, timeout=0.3)
        self.assertTrue(slow_started.is_set())
        release.set()
        for index in range(5):
            self.assertEqual(
                worker.call(lambda index=index: f"fresh-{index}", timeout=5),
                f"fresh-{index}",
            )

    def test_fire_and_forget_drop_is_bounded(self) -> None:
        worker = BrowserWorker(name="stress-worker-bounded", max_queue_size=2)
        accepted = 0
        for _ in range(20):
            if worker.submit(lambda: None):
                accepted += 1
        snapshot = worker.health_snapshot()
        self.assertLessEqual(snapshot.queue_size, 2)
        self.assertGreaterEqual(accepted, 1)


if __name__ == "__main__":
    unittest.main()
