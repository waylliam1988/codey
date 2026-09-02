from __future__ import annotations

import threading
import time
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

    def test_reentrant_call_honors_timeout_and_scopes(self) -> None:
        from codey.runtime import cancellation

        worker = browser_worker.BrowserWorker(name="test-reentrant-worker")

        def outer_job() -> None:
            def inner_job() -> int:
                return 999

            res = worker.call(inner_job, timeout=1.0)
            self.assertEqual(res, 999)

            deadline = cancellation.current_deadline()
            self.assertIsNotNone(deadline)

        worker.call(outer_job, timeout=2.0)

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

        self.assertIn("job abandoned", str(ctx.exception))

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

    def test_health_snapshot_reports_idle_metrics(self) -> None:
        worker = browser_worker.BrowserWorker(name="test-health-worker")

        self.assertEqual(worker.call(lambda: 7), 7)
        health = worker.health_snapshot()

        self.assertEqual(health.state, "idle")
        self.assertEqual(health.current_job_state, "")
        self.assertFalse(health.stuck_detected)
        self.assertGreaterEqual(health.completed_jobs, 1)
        self.assertEqual(health.failed_jobs, 0)
        self.assertTrue(health.thread_alive)
        self.assertEqual(health.to_payload()["state"], "idle")

    def test_health_snapshot_detects_stuck_cancel_requested_job(self) -> None:
        worker = browser_worker.BrowserWorker(
            name="test-stuck-health-worker",
            stuck_after_seconds=0.01,
        )
        started = threading.Event()
        release = threading.Event()
        errors: list[Exception] = []

        def blocking_job() -> None:
            started.set()
            release.wait(timeout=2.0)

        def caller() -> None:
            try:
                worker.call(blocking_job, timeout=0.05)
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=caller)
        thread.start()
        self.assertTrue(started.wait(2.0))
        thread.join(timeout=1.0)
        self.assertTrue(any(isinstance(exc, TimeoutError) for exc in errors))

        time.sleep(0.03)
        health = worker.health_snapshot()

        self.assertEqual(health.state, "stuck")
        self.assertEqual(health.current_job_state, "abandoned")
        self.assertTrue(health.stuck_detected)
        self.assertGreaterEqual(health.running_for_seconds, health.stuck_after_seconds)

        probe_ran = threading.Event()
        worker.submit(probe_ran.set)
        self.assertFalse(probe_ran.wait(0.05))
        self.assertGreaterEqual(worker.health_snapshot().queue_size, 1)

        release.set()
        self.assertTrue(probe_ran.wait(2.0))
        self.assertEqual(worker.call(lambda: 123, timeout=2.0), 123)

    def test_running_job_timeout_abandons_and_discards_late_result(self) -> None:
        worker = browser_worker.BrowserWorker(name="test-abandon-worker")
        started = threading.Event()
        finish_slow_work = threading.Event()
        slow_work_finished = threading.Event()

        def slow_job() -> str:
            started.set()
            finish_slow_work.wait(timeout=2.0)
            slow_work_finished.set()
            return "late_success_value"

        with self.assertRaises(TimeoutError) as ctx:
            worker.call(slow_job, timeout=0.05)
        self.assertIn("job abandoned", str(ctx.exception))

        # Allow slow job to finish in the background
        finish_slow_work.set()
        self.assertTrue(slow_work_finished.wait(2.0))

        # Subsequent call receives its own result, completely unpolluted by the late result
        res = worker.call(lambda: "fresh_job_value", timeout=2.0)
        self.assertEqual(res, "fresh_job_value")


if __name__ == "__main__":
    unittest.main()
