from __future__ import annotations

import threading
import unittest

from codey.app.run_registry import RunRegistry, same_project


class RunRegistryTests(unittest.TestCase):
    def test_reserve_is_atomic(self) -> None:
        registry = RunRegistry()
        barrier = threading.Barrier(8)
        results = []

        def reserve() -> None:
            barrier.wait()
            results.append(
                registry.reserve(
                    session_id="session-1",
                    project=None,
                    task="hello",
                    provider_id="deepseek",
                )
            )

        threads = [threading.Thread(target=reserve) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        accepted = [item for item in results if item is not None]
        self.assertEqual(len(accepted), 1)
        self.assertTrue(registry.is_busy())
        self.assertEqual(registry.current(), accepted[0])

    def test_stop_flag_survives_start_after_reservation(self) -> None:
        registry = RunRegistry()
        registry.stop_flag.set()
        run = registry.reserve(
            session_id="session-1",
            project=None,
            task="hello",
            provider_id="deepseek",
        )
        assert run is not None
        self.assertFalse(registry.stop_flag.is_set())

        registry.stop_flag.set()
        self.assertTrue(registry.start(run.run_id))

        self.assertTrue(registry.stop_flag.is_set())

    def test_abort_if_stopped_leaves_slot_free_and_flag_set(self) -> None:
        registry = RunRegistry()
        registry.stop_flag.set()

        run = registry.reserve(
            session_id="session-1",
            project=None,
            task="hello",
            provider_id="deepseek",
            abort_if_stopped=True,
        )

        self.assertIsNone(run)
        self.assertTrue(registry.stop_flag.is_set())
        self.assertIsNone(registry.current())
        self.assertFalse(registry.is_busy())

    def test_wait_for_slot_tracks_authoritative_slot_state(self) -> None:
        registry = RunRegistry()

        self.assertTrue(registry.wait_for_slot(0.0))
        registry.set_busy(True)
        self.assertFalse(registry.wait_for_slot(0.0))
        registry.set_busy(False)
        self.assertTrue(registry.wait_for_slot(0.0))

        run = registry.reserve(
            session_id="session-1",
            project=None,
            task="hello",
            provider_id="deepseek",
        )
        assert run is not None
        self.assertFalse(registry.wait_for_slot(0.0))

        registry.set_busy(False)
        self.assertFalse(registry.wait_for_slot(0.0))
        registry.release(run.run_id)
        self.assertTrue(registry.wait_for_slot(0.0))

    def test_finish_returns_terminal_payload_and_releases_slot(self) -> None:
        registry = RunRegistry()
        run = registry.reserve(
            session_id="session-1",
            project="E:/demo",
            task="hello",
            provider_id="qwen",
        )
        assert run is not None
        self.assertTrue(registry.start(run.run_id))

        payload = registry.finish(
            run.run_id,
            {"type": "task_done", "stop_reason": "done", "summary": "ok"},
        )

        assert payload is not None
        self.assertFalse(registry.is_busy())
        self.assertIsNone(registry.current())
        self.assertEqual(payload["run_id"], run.run_id)
        self.assertEqual(payload["session_id"], "session-1")
        self.assertEqual(registry.last_terminal_event(), payload)
        self.assertEqual(registry.status(), "done")

    def test_payload_keeps_pending_event_and_research_restore_refs(self) -> None:
        registry = RunRegistry()
        run = registry.reserve(
            session_id="session-1",
            project="E:/demo",
            task="hello",
            provider_id="qwen",
        )
        assert run is not None
        registry.start(run.run_id)
        pending = {"type": "shell_request", "run_id": run.run_id}

        payload = registry.payload(
            pending_event=(
                lambda active: pending
                if active is not None and active.run_id == run.run_id
                else None
            ),
            research_restore_runs=("run-a",),
        )

        self.assertTrue(payload["busy"])
        self.assertEqual(payload["run_id"], run.run_id)
        self.assertEqual(payload["pending_event"], pending)
        self.assertEqual(payload["research_restore_runs"], ["run-a"])

    def test_payload_does_not_call_pending_event_under_registry_lock(self) -> None:
        registry = RunRegistry()
        run = registry.reserve(
            session_id="session-1",
            project="E:/demo",
            task="hello",
            provider_id="qwen",
        )
        assert run is not None
        registry.start(run.run_id)
        payloads: list[dict] = []
        errors: list[BaseException] = []

        def pending_event(_active) -> dict | None:
            current = registry.current()
            return {"type": "shell_request", "run_id": current.run_id if current else ""}

        def build_payload() -> None:
            try:
                payloads.append(
                    registry.payload(
                        pending_event=pending_event,
                        research_restore_runs=(),
                    )
                )
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=build_payload)
        thread.start()
        thread.join(timeout=1.0)

        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(payloads[0]["pending_event"]["run_id"], run.run_id)

    def test_same_project_compares_resolved_paths(self) -> None:
        self.assertTrue(same_project(".", "."))
        self.assertFalse(same_project("", "."))


if __name__ == "__main__":
    unittest.main()
