from __future__ import annotations

import threading
import unittest

from codey.automation import browser_worker


class BrowserWorkerTests(unittest.TestCase):
    def test_runs_callable_on_worker_thread(self) -> None:
        seen: list[int] = []

        def job() -> int:
            seen.append(threading.get_ident())
            return 42

        worker_id = browser_worker.WORKER.call(lambda: threading.get_ident())
        result = browser_worker.WORKER.call(job)

        self.assertEqual(result, 42)
        self.assertEqual(seen, [worker_id])

    def test_submit_does_not_block_caller(self) -> None:
        done = threading.Event()

        def job() -> None:
            done.set()

        browser_worker.submit(job)
        self.assertTrue(done.wait(2.0))


if __name__ == "__main__":
    unittest.main()