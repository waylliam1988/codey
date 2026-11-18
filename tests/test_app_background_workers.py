from __future__ import annotations

import threading
import unittest

from codey.app.knowledge_indexer import KnowledgeIndexer


class KnowledgeIndexerTests(unittest.TestCase):
    def test_schedule_rebuilds_once_and_runs_pending_pass(self) -> None:
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
        indexer = KnowledgeIndexer(store=lambda: store)

        indexer.schedule()
        self.assertTrue(release.wait(2))
        for _ in range(100):
            with indexer.lock:
                running = indexer.running
            if not running:
                break
            threading.Event().wait(0.02)

        self.assertEqual(calls, 2)

    def test_worker_errors_are_recorded_without_staying_running(self) -> None:
        class Store:
            def rebuild(self) -> None:
                raise RuntimeError("index failed")

        indexer = KnowledgeIndexer(store=Store)

        indexer.schedule()
        for _ in range(100):
            with indexer.lock:
                running = indexer.running
            if not running:
                break
            threading.Event().wait(0.02)

        self.assertFalse(indexer.running)
        self.assertEqual(indexer.error_count, 1)
        self.assertEqual(indexer.last_error, "RuntimeError: index failed")
        self.assertTrue(indexer.last_error_ref.startswith("sha256:"))


if __name__ == "__main__":
    unittest.main()
