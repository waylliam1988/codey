from __future__ import annotations

import threading
import unittest

from codey.app.ghost_daemon import GhostSleepDaemon
from codey.app.knowledge_indexer import KnowledgeIndexer


class GhostSleepDaemonTests(unittest.TestCase):
    def test_busy_kick_records_pending_payload_without_starting_thread(self) -> None:
        lock = threading.Lock()
        calls: list[dict[str, object]] = []
        daemon = GhostSleepDaemon(
            lock=lock,
            is_busy=lambda: True,
            stop_requested=lambda: False,
            run_once=lambda payload: calls.append(payload),
        )

        kicked = daemon.kick({"run_id": "run-1"})

        self.assertFalse(kicked)
        self.assertEqual(calls, [])
        self.assertTrue(daemon.pending)
        self.assertEqual(daemon.pending_payload, {"run_id": "run-1"})

    def test_running_daemon_processes_latest_pending_payload(self) -> None:
        lock = threading.Lock()
        release = threading.Event()
        calls: list[dict[str, object]] = []
        daemon: GhostSleepDaemon

        def run_once(payload: dict[str, object]) -> None:
            calls.append(dict(payload))
            if len(calls) == 1:
                daemon.kick({"run_id": "run-2"})
                daemon.kick({"run_id": "run-3"})
                release.set()

        daemon = GhostSleepDaemon(
            lock=lock,
            is_busy=lambda: False,
            stop_requested=lambda: False,
            run_once=run_once,
        )

        self.assertTrue(daemon.kick({"run_id": "run-1"}))
        self.assertTrue(release.wait(2))
        self.assertTrue(daemon.wait(2))

        self.assertEqual([payload["run_id"] for payload in calls], ["run-1", "run-3"])

    def test_worker_errors_are_recorded_without_staying_running(self) -> None:
        lock = threading.Lock()
        daemon = GhostSleepDaemon(
            lock=lock,
            is_busy=lambda: False,
            stop_requested=lambda: False,
            run_once=lambda _payload: (_ for _ in ()).throw(RuntimeError("sleep failed")),
        )

        self.assertTrue(daemon.kick({"run_id": "run-1"}))
        self.assertTrue(daemon.wait(2))

        self.assertFalse(daemon.running)
        self.assertEqual(daemon.error_count, 1)
        self.assertEqual(daemon.last_error, "RuntimeError: sleep failed")
        self.assertTrue(daemon.last_error_ref.startswith("sha256:"))


class KnowledgeIndexerTests(unittest.TestCase):
    def test_schedule_rebuilds_once_and_runs_pending_pass(self) -> None:
        lock = threading.Lock()
        release = threading.Event()
        calls = 0
        indexer: KnowledgeIndexer

        class Store:
            def rebuild(self) -> None:
                nonlocal calls
                calls += 1
                if calls == 1:
                    indexer.schedule()
                    release.set()

        store = Store()
        indexer = KnowledgeIndexer(lock=lock, store=lambda: store)

        indexer.schedule()
        self.assertTrue(release.wait(2))
        for _ in range(100):
            with lock:
                running = indexer.running
            if not running:
                break
            threading.Event().wait(0.02)

        self.assertEqual(calls, 2)


if __name__ == "__main__":
    unittest.main()
