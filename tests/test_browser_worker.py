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

        worker_id = browser_worker.default_worker().call(lambda: threading.get_ident())
        result = browser_worker.default_worker().call(job)

        self.assertEqual(result, 42)
        self.assertEqual(seen, [worker_id])

    def test_submit_does_not_block_caller(self) -> None:
        done = threading.Event()

        def job() -> None:
            done.set()

        browser_worker.submit(job)
        self.assertTrue(done.wait(2.0))

    def _track_worker(self, worker: browser_worker.BrowserWorker) -> browser_worker.BrowserWorker:
        def _close_and_assert() -> None:
            self.assertTrue(worker.close())
            self.assertFalse(worker._thread.is_alive())
        self.addCleanup(_close_and_assert)
        return worker

    def test_close_completes_running_and_queued_jobs(self) -> None:
        worker = browser_worker.BrowserWorker(name="test-close-terminal", max_queue_size=8)
        # Track manually: this close is the assertion itself.
        running_started = threading.Event()

        def _running() -> str:
            running_started.set()
            time.sleep(0.2)
            return "running-done"

        runner_errors: list[BaseException] = []

        def _run_running() -> None:
            try:
                worker.call(_running, timeout=10.0)
            except BaseException as exc:
                runner_errors.append(exc)

        runner = threading.Thread(target=_run_running, daemon=True)
        runner.start()
        self.assertTrue(running_started.wait(timeout=10.0))
        queued_errors: list[BaseException] = []

        def _run_queued() -> None:
            try:
                worker.call(lambda: "queued-never", timeout=None)
            except BaseException as exc:
                queued_errors.append(exc)

        queued = threading.Thread(target=_run_queued, daemon=True)
        queued.start()
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and worker.health_snapshot().queue_size < 1:
            time.sleep(0.01)
        self.assertGreaterEqual(worker.health_snapshot().queue_size, 1)
        self.assertTrue(worker.close(timeout=5.0))
        runner.join(timeout=10.0)
        queued.join(timeout=10.0)
        self.assertFalse(runner.is_alive())
        self.assertFalse(queued.is_alive())
        self.assertFalse(worker._thread.is_alive())
        self.assertEqual(len(queued_errors), 1)
        self.assertIsInstance(queued_errors[0], RuntimeError)
        self.assertIn("closed", str(queued_errors[0]))
        # Every accepted job got a terminal state, never a silent hang.
        self.assertGreaterEqual(worker.health_snapshot().cancelled_jobs, 1)

    def test_submit_vs_close_is_atomic(self) -> None:
        worker = browser_worker.BrowserWorker(name="test-submit-close-race", max_queue_size=8)
        outcomes: list[str] = []
        stop = threading.Event()

        def _submit_loop() -> None:
            while not stop.is_set():
                try:
                    accepted = worker.submit(lambda: None)
                    outcomes.append("accepted" if accepted else "dropped")
                except RuntimeError:
                    outcomes.append("closed-error")
                    return

        submitter = threading.Thread(target=_submit_loop, daemon=True)
        submitter.start()
        time.sleep(0.1)
        self.assertTrue(worker.close(timeout=5.0))
        stop.set()
        submitter.join(timeout=10.0)
        self.assertFalse(submitter.is_alive())
        self.assertIn("closed-error", outcomes)
        self.assertFalse(worker._thread.is_alive())

    def test_close_does_not_run_browser_cleanup_for_unstarted_job(self) -> None:
        worker = browser_worker.BrowserWorker(name="test-close-async", max_queue_size=8)
        running_started = threading.Event()
        running_release = threading.Event()

        def _blocking() -> None:
            running_started.set()
            running_release.wait(timeout=10.0)

        worker.submit(_blocking)
        self.assertTrue(running_started.wait(timeout=10.0))
        executed: list[bool] = []
        cleaned: list[bool] = []
        worker.submit(
            lambda: executed.append(True), on_abandoned=lambda: cleaned.append(True)
        )
        close_out: list[bool] = []

        def _do_close() -> None:
            close_out.append(worker.close(timeout=5.0))

        closer = threading.Thread(target=_do_close, daemon=True)
        closer.start()
        running_release.set()
        closer.join(timeout=10.0)
        self.assertFalse(closer.is_alive())
        self.assertEqual(close_out, [True])
        self.assertFalse(worker._thread.is_alive())
        self.assertEqual(executed, [])
        self.assertEqual(cleaned, [])
        self.assertGreaterEqual(worker.health_snapshot().cancelled_jobs, 1)

    def test_running_job_abandon_cleanup_stays_on_browser_thread(self) -> None:
        worker = browser_worker.BrowserWorker(name="test-close-running-cleanup")
        started = threading.Event()
        release = threading.Event()
        cleaned_on: list[int] = []

        def running() -> None:
            started.set()
            release.wait(timeout=10.0)

        worker.submit(running, on_abandoned=lambda: cleaned_on.append(threading.get_ident()))
        self.assertTrue(started.wait(timeout=10.0))
        try:
            self.assertFalse(worker.close(timeout=0.01))
        finally:
            release.set()
        self.assertTrue(worker.close(timeout=5.0))
        self.assertEqual(cleaned_on, [worker._thread_id])
        self.assertNotEqual(cleaned_on, [threading.get_ident()])

    def test_close_timeout_reports_incomplete_with_hung_job(self) -> None:
        worker = browser_worker.BrowserWorker(name="test-close-hung", max_queue_size=8)
        running_started = threading.Event()
        running_release = threading.Event()

        def _hung() -> str:
            running_started.set()
            running_release.wait(timeout=30.0)
            return "hung-done"

        hung_errors: list[BaseException] = []

        def _run_hung() -> None:
            try:
                worker.call(_hung, timeout=30.0)
            except BaseException as exc:
                hung_errors.append(exc)

        hung = threading.Thread(target=_run_hung, daemon=True)
        hung.start()
        self.assertTrue(running_started.wait(timeout=10.0))
        queued_errors: list[BaseException] = []

        def _run_queued() -> None:
            try:
                worker.call(lambda: "queued-never", timeout=None)
            except BaseException as exc:
                queued_errors.append(exc)

        queued = threading.Thread(target=_run_queued, daemon=True)
        queued.start()
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and worker.health_snapshot().queue_size < 1:
            time.sleep(0.01)
        self.assertGreaterEqual(worker.health_snapshot().queue_size, 1)
        self.assertFalse(worker.close(timeout=0.3))
        self.assertTrue(worker._thread.is_alive())
        queued.join(timeout=10.0)
        self.assertFalse(queued.is_alive())
        self.assertEqual(len(queued_errors), 1)
        self.assertIsInstance(queued_errors[0], RuntimeError)
        self.assertIn("closed", str(queued_errors[0]))
        running_release.set()
        hung.join(timeout=10.0)
        self.assertFalse(hung.is_alive())
        self.assertEqual(len(hung_errors), 1)
        self.assertIsInstance(hung_errors[0], RuntimeError)
        self.assertIn("closed", str(hung_errors[0]))
        self.assertTrue(worker.close(timeout=5.0))
        self.assertFalse(worker._thread.is_alive())

    def test_reentrant_call_honors_timeout_and_scopes(self) -> None:
        from codey.runtime.core import cancellation

        worker = self._track_worker(browser_worker.BrowserWorker(name="test-reentrant-worker"))

        def outer_job() -> None:
            def inner_job() -> int:
                return 999

            res = worker.call(inner_job, timeout=1.0)
            self.assertEqual(res, 999)

            deadline = cancellation.current_deadline()
            self.assertIsNotNone(deadline)

        worker.call(outer_job, timeout=2.0)

    def test_call_timeout_cancels_queued_job_and_raises_timeout_error(self) -> None:
        worker = self._track_worker(browser_worker.BrowserWorker(name="test-timeout-worker"))
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
        from codey.runtime.core import cancellation

        worker = self._track_worker(browser_worker.BrowserWorker(name="test-cancel-worker"))
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
        worker = self._track_worker(browser_worker.BrowserWorker(name="test-health-worker"))

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
        worker = self._track_worker(
            browser_worker.BrowserWorker(
                name="test-stuck-health-worker",
                stuck_after_seconds=0.01,
            )
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
        worker = self._track_worker(browser_worker.BrowserWorker(name="test-abandon-worker"))
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
