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
    def test_call_timeout_cancels_queued_job_and_raises_timeout_error(self) -> None:
        worker = browser_worker.BrowserWorker(name="test-timeout-worker")
        blocker_started = threading.Event()
        unblock = threading.Event()

        def blocking_job() -> None:
            blocker_started.set()
            unblock.wait(timeout=2.0)

        worker.submit(blocking_job)
        self.assertTrue(blocker_started.wait(2.0))

        queued_job_executed = False

        def queued_job() -> None:
            nonlocal queued_job_executed
            queued_job_executed = True

        with self.assertRaises(TimeoutError) as ctx:
            worker.call(queued_job, timeout=0.05)

        self.assertIn("cancellation requested", str(ctx.exception))

        unblock.set()
        # Give worker a moment to process the remaining queue
        empty_job = worker.call(lambda: 123)
        self.assertEqual(empty_job, 123)
        self.assertFalse(queued_job_executed)

    def test_running_job_observes_cancellation_scope(self) -> None:
        from codey.runtime import cancellation

        worker = browser_worker.BrowserWorker(name="test-cancel-worker")
        job_started = threading.Event()
        saw_cancellation = threading.Event()

        def cancellable_job() -> None:
            job_started.set()
            for _ in range(50):
                if cancellation.current_event() and cancellation.current_event().is_set():
                    saw_cancellation.set()
                    break
                import time

                time.sleep(0.01)

        caller_cancel = threading.Event()
        worker_error: list[Exception] = []

        def caller() -> None:
            try:
                with cancellation.scope(caller_cancel):
                    worker.call(cancellable_job, timeout=1.0)
            except Exception as exc:
                worker_error.append(exc)

        t = threading.Thread(target=caller)
        t.start()
        self.assertTrue(job_started.wait(2.0))

        # Cancel caller
        caller_cancel.set()
        t.join(timeout=2.0)

        self.assertTrue(saw_cancellation.wait(2.0))
        self.assertTrue(any(isinstance(exc, cancellation.TaskCancelled) for exc in worker_error))


if __name__ == "__main__":
    unittest.main()