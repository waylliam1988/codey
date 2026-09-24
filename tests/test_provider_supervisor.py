from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from codey.providers.diagnostics import FAILURE_READINESS_STALE, ProviderFailure
from codey.providers.supervisor import (
    STATE_AUTH_REQUIRED,
    STATE_DEGRADED,
    STATE_HEALTHY,
    STATE_OPEN,
    ProviderSupervisor,
    run_half_open_canary,
)
from codey.runtime.core import cancellation


def failure(kind: str) -> ProviderFailure:
    return ProviderFailure("Qwen", "send", "secret-url", "title", "body", "now", kind)


class ProviderSupervisorTests(unittest.TestCase):
    def test_success_and_structural_circuit_transitions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            supervisor = ProviderSupervisor(td, clock=lambda: 100.0)
            self.assertEqual(supervisor.record_success("qwen").state, STATE_HEALTHY)
            self.assertEqual(
                supervisor.record_failure("qwen", failure("control_missing")).state,
                STATE_DEGRADED,
            )
            opened = supervisor.record_failure("qwen", failure("response_missing"))

            self.assertEqual(opened.state, STATE_OPEN)
            self.assertTrue(supervisor.allows_revival("qwen"))
            self.assertFalse(supervisor.is_available("qwen"))

    def test_readiness_stale_is_structural_for_circuit_and_revival(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            supervisor = ProviderSupervisor(td, clock=lambda: 100.0)

            self.assertEqual(
                supervisor.record_failure("qwen", failure(FAILURE_READINESS_STALE)).state,
                STATE_DEGRADED,
            )
            opened = supervisor.record_failure("qwen", failure(FAILURE_READINESS_STALE))

            self.assertEqual(opened.state, STATE_OPEN)
            self.assertTrue(supervisor.allows_revival("qwen"))

    def test_expired_circuit_recovers_as_degraded_after_restart(self) -> None:
        now = [100.0]
        with tempfile.TemporaryDirectory() as td:
            supervisor = ProviderSupervisor(td, clock=lambda: now[0])
            supervisor.record_failure("qwen", failure("rate_limited"))
            now[0] = 500.0

            restarted = ProviderSupervisor(td, clock=lambda: now[0])

            self.assertEqual(restarted.get("qwen").state, STATE_DEGRADED)
            self.assertTrue(restarted.needs_canary("qwen"))

    def test_auth_and_challenge_require_user_action(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            supervisor = ProviderSupervisor(td)
            self.assertEqual(
                supervisor.record_failure(
                    "deepseek", failure("authentication_required")
                ).state,
                STATE_AUTH_REQUIRED,
            )
            self.assertFalse(supervisor.is_available("deepseek"))
            self.assertEqual(
                supervisor.prepare_user_selected("deepseek").state,
                STATE_DEGRADED,
            )
            self.assertTrue(supervisor.needs_canary("deepseek"))

    def test_submission_uncertain_is_degraded_not_permanently_open(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            supervisor = ProviderSupervisor(td)
            health = supervisor.record_failure(
                "stepfun", failure("submission_uncertain")
            )

            self.assertEqual(health.state, STATE_DEGRADED)
            self.assertTrue(supervisor.is_available("stepfun"))

    def test_selection_is_deterministic_and_skips_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            supervisor = ProviderSupervisor(td, clock=lambda: 100.0)
            supervisor.record_failure("qwen", failure("rate_limited"))

            selected = supervisor.select(
                "qwen",
                ("deepseek", "stepfun", "glm"),
                excluded=("deepseek",),
            )

            self.assertEqual(selected, "stepfun")

    def test_corrupt_file_degrades_to_empty_state(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "provider-health.json"
            path.write_text("{broken", encoding="utf-8")

            supervisor = ProviderSupervisor(td)

            self.assertTrue(supervisor.is_available("qwen"))

    def test_persistence_contains_only_bounded_health_fields(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            supervisor = ProviderSupervisor(td)
            supervisor.record_failure("qwen", failure("control_missing"))

            text = (Path(td) / "provider-health.json").read_text(encoding="utf-8")
            payload = json.loads(text)

            self.assertIn("providers", payload)
            self.assertNotIn("secret-url", text)
            self.assertNotIn("body", text)
            self.assertNotIn("title", text)

    def test_half_open_canary_contains_no_project_or_user_content(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            now = [100.0]
            supervisor = ProviderSupervisor(td, clock=lambda: now[0])
            supervisor.record_failure("qwen", failure("rate_limited"))
            now[0] = 500.0
            provider = mock.Mock()
            provider.send.side_effect = (
                lambda prompt, timeout: prompt.rsplit(" ", 1)[-1]
            )

            ok = run_half_open_canary("qwen", provider, supervisor)

            self.assertTrue(ok)
            self.assertGreater(provider.new_chat.call_args.kwargs["timeout"], 0)
            prompt = provider.send.call_args.args[0]
            self.assertGreater(provider.send.call_args.kwargs["timeout"], 0)
            self.assertIn("SESSION_CHECK_", prompt)
            self.assertNotIn("codey", prompt.lower())
            self.assertNotIn("project", prompt.lower())
            self.assertNotIn("user", prompt.lower())
            self.assertEqual(supervisor.get("qwen").state, STATE_DEGRADED)
            self.assertFalse(supervisor.needs_canary("qwen"))

            self.assertTrue(run_half_open_canary("qwen", provider, supervisor))
            provider.send.assert_called_once()

    def test_failed_half_open_canary_reopens_circuit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            now = [100.0]
            supervisor = ProviderSupervisor(td, clock=lambda: now[0])
            supervisor.record_failure("qwen", failure("rate_limited"))
            now[0] = 500.0
            provider = mock.Mock()
            provider.send.return_value = "wrong"

            self.assertFalse(run_half_open_canary("qwen", provider, supervisor))
            self.assertEqual(supervisor.get("qwen").state, STATE_OPEN)

    def test_half_open_canary_propagates_user_stop(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            now = [100.0]
            supervisor = ProviderSupervisor(td, clock=lambda: now[0])
            supervisor.record_failure("qwen", failure("rate_limited"))
            now[0] = 500.0
            provider = mock.Mock()
            provider.new_chat.side_effect = cancellation.TaskCancelled("stopped")

            with self.assertRaises(cancellation.TaskCancelled):
                run_half_open_canary("qwen", provider, supervisor)

            provider.send.assert_not_called()

    def test_half_open_canary_shares_one_deadline_between_actions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            now = [100.0]
            supervisor = ProviderSupervisor(td, clock=lambda: now[0])
            supervisor.record_failure("qwen", failure("rate_limited"))
            now[0] = 500.0
            provider = mock.Mock()
            provider.send.side_effect = (
                lambda prompt, timeout: prompt.rsplit(" ", 1)[-1]
            )

            with (
                mock.patch(
                    "codey.providers.supervisor.start_deadline",
                    return_value=123.0,
                ),
                mock.patch(
                    "codey.providers.supervisor.remaining",
                    side_effect=[30.0, 5.0],
                ) as remaining_budget,
                mock.patch("codey.providers.supervisor.cancellation.deadline_scope") as scope,
            ):
                scope.return_value.__enter__.return_value = None
                scope.return_value.__exit__.return_value = False
                self.assertTrue(run_half_open_canary("qwen", provider, supervisor))

            provider.new_chat.assert_called_once_with(timeout=30.0)
            self.assertEqual(provider.send.call_args.kwargs["timeout"], 5.0)
            self.assertEqual(
                remaining_budget.call_args_list,
                [mock.call(123.0, 45.0), mock.call(123.0, 45.0)],
            )

    def test_failure_counter_resets_when_failure_family_changes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            supervisor = ProviderSupervisor(td, clock=lambda: 100.0)
            supervisor.record_failure("qwen", failure("transient"))
            supervisor.record_failure("qwen", failure("transient"))

            structural = supervisor.record_failure(
                "qwen", failure("control_missing")
            )
            opened = supervisor.record_failure(
                "qwen", failure("response_missing")
            )

            self.assertEqual(structural.consecutive_failures, 1)
            self.assertEqual(structural.state, STATE_DEGRADED)
            self.assertEqual(opened.consecutive_failures, 2)
            self.assertEqual(opened.state, STATE_OPEN)

    def test_concurrent_health_updates_do_not_lose_counts(self) -> None:
        supervisor = ProviderSupervisor()

        def record_successes() -> None:
            for _ in range(50):
                supervisor.record_success("qwen")

        threads = [threading.Thread(target=record_successes) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(supervisor.get("qwen").success_count, 400)

    def test_concurrent_transition_and_success_keep_disk_fresh(self) -> None:
        import time as _time

        from codey.providers import supervisor as supervisor_module

        with tempfile.TemporaryDirectory() as td:
            supervisor = ProviderSupervisor(td, clock=lambda: 100.0)
            supervisor.record_failure("qwen", failure("response_missing"))
            supervisor.record_failure("qwen", failure("response_missing"))
            self.assertEqual(supervisor.get("qwen").state, STATE_OPEN)

            original_write = supervisor_module.ProviderSupervisor._write_snapshot
            entered_write = threading.Event()
            writer_started = threading.Event()
            entered_once = {"done": False}

            def slow_write(inner_self, snapshot) -> None:
                if not entered_once["done"]:
                    entered_once["done"] = True
                    entered_write.set()
                    # Both latches: wait until the writer attempts its update,
                    # then hold briefly so it lands after the transition
                    # write. Both sides serialize on the same locks, so the
                    # success always applies to the fresh DEGRADED state.
                    assert writer_started.wait(timeout=10.0)
                    _time.sleep(0.2)
                original_write(inner_self, snapshot)

            def do_success() -> None:
                writer_started.set()
                supervisor.record_success("qwen")

            with mock.patch.object(
                supervisor_module.ProviderSupervisor, "_write_snapshot", slow_write,
            ):
                def do_transition() -> None:
                    supervisor.clock = lambda: 1000.0
                    supervisor.get("qwen")

                transition = threading.Thread(target=do_transition)
                transition.start()
                self.assertTrue(entered_write.wait(timeout=10.0))
                writer = threading.Thread(target=do_success)
                writer.start()
                writer.join(timeout=10.0)
                transition.join(timeout=10.0)
                self.assertFalse(writer.is_alive())

            self.assertFalse(transition.is_alive())
            self.assertEqual(supervisor.get("qwen").state, STATE_HEALTHY)
            reloaded = ProviderSupervisor(td, clock=lambda: 1000.0)
            self.assertEqual(reloaded.get("qwen").state, STATE_HEALTHY)

    def test_two_instances_sharing_state_dir_keep_each_other(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            first = ProviderSupervisor(td, clock=lambda: 100.0)
            second = ProviderSupervisor(td, clock=lambda: 100.0)
            first.record_success("qwen")
            second.record_success("glm")
            reloaded = ProviderSupervisor(td, clock=lambda: 100.0)
            self.assertEqual(reloaded.get("qwen").state, STATE_HEALTHY)
            self.assertEqual(reloaded.get("glm").state, STATE_HEALTHY)

    def test_same_id_failures_accumulate_across_instances(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            first = ProviderSupervisor(td, clock=lambda: 100.0)
            second = ProviderSupervisor(td, clock=lambda: 100.0)
            first.record_failure("qwen", failure("control_missing"))
            second.record_failure("qwen", failure("response_missing"))
            reloaded = ProviderSupervisor(td, clock=lambda: 100.0)
            health = reloaded.get("qwen")
            self.assertEqual(health.consecutive_failures, 2)
            self.assertEqual(health.failure_count, 2)
            self.assertEqual(health.state, STATE_OPEN)

    def test_same_id_success_resets_across_instances(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            first = ProviderSupervisor(td, clock=lambda: 100.0)
            second = ProviderSupervisor(td, clock=lambda: 100.0)
            first.record_failure("qwen", failure("control_missing"))
            second.record_failure("qwen", failure("response_missing"))
            self.assertEqual(second.get("qwen").state, STATE_OPEN)
            first.record_success("qwen")
            reloaded = ProviderSupervisor(td, clock=lambda: 100.0)
            health = reloaded.get("qwen")
            self.assertEqual(health.state, STATE_HEALTHY)
            self.assertEqual(health.consecutive_failures, 0)
            self.assertEqual(health.failure_count, 2)
            self.assertEqual(health.success_count, 1)

    def test_stale_instance_select_sees_fresh_circuit_and_expiry(self) -> None:
        now = [100.0]
        with tempfile.TemporaryDirectory() as td:
            early = ProviderSupervisor(td, clock=lambda: now[0])
            late = ProviderSupervisor(td, clock=lambda: now[0])
            late.record_failure("qwen", failure("rate_limited"))
            # Created before the failure: must still route around the OPEN
            # circuit using fresh disk state, not its stale empty cache.
            self.assertEqual(early.select("qwen", ("glm",)), "glm")
            self.assertFalse(early.is_available("qwen"))
            now[0] = 500.0
            # Expired circuits cool down for every instance without a nudge.
            self.assertEqual(early.get("qwen").state, STATE_DEGRADED)
            self.assertEqual(early.select("qwen", ("glm",)), "qwen")

    def test_write_failure_is_explicit_and_rolls_back(self) -> None:
        from codey.providers import supervisor as supervisor_module

        with tempfile.TemporaryDirectory() as td:
            supervisor = ProviderSupervisor(td, clock=lambda: 100.0)
            with (
                mock.patch.object(
                    supervisor_module,
                    "write_json_atomic",
                    side_effect=OSError("disk full"),
                ),
                self.assertRaises(supervisor_module.HealthStoreError),
            ):
                supervisor.record_failure("qwen", failure("rate_limited"))
            # Never reported as recorded: memory matches the durable state,
            # so "still available" does not contradict a claimed breaker.
            self.assertNotIn("qwen", supervisor._health)
            self.assertNotEqual(supervisor.last_save_error, "")
            self.assertEqual(supervisor.get("qwen").state, "unknown")
            self.assertTrue(supervisor.is_available("qwen"))
            self.assertFalse((Path(td) / "provider-health.json").exists())

    def test_transient_read_failure_raises_without_resetting(self) -> None:
        from codey.providers import supervisor as supervisor_module
        from codey.storage.local_store import StoreCorruption

        with tempfile.TemporaryDirectory() as td:
            supervisor = ProviderSupervisor(td, clock=lambda: 100.0)
            supervisor.record_success("qwen")
            manifest = Path(td) / "provider-health.json"
            self.assertTrue(manifest.is_file())

            def unreadable(path: Path, *, max_bytes: int) -> dict:
                raise StoreCorruption(path, "PermissionError")

            with (
                mock.patch.object(
                    supervisor_module, "read_json_strict", side_effect=unreadable,
                ),
                self.assertRaises(supervisor_module.HealthStoreError),
            ):
                supervisor.get("qwen")
            # A read fault is not corruption: the file is untouched, no
            # backup is taken, and the next readable access heals.
            self.assertTrue(manifest.is_file())
            self.assertFalse(manifest.with_name(manifest.name + ".corrupt").exists())
            self.assertEqual(supervisor.get("qwen").state, STATE_HEALTHY)

    def test_unreadable_store_at_startup_starts_empty_but_visible(self) -> None:
        from codey.providers import supervisor as supervisor_module
        from codey.storage.local_store import StoreCorruption

        with tempfile.TemporaryDirectory() as td:
            manifest = Path(td) / "provider-health.json"
            manifest.write_text('{"schema_version": 1, "providers": {}}', encoding="utf-8")

            def unreadable(path: Path, *, max_bytes: int) -> dict:
                raise StoreCorruption(path, "PermissionError")

            with mock.patch.object(
                supervisor_module, "read_json_strict", side_effect=unreadable,
            ):
                supervisor = ProviderSupervisor(td, clock=lambda: 100.0)
            self.assertNotEqual(supervisor.last_save_error, "")
            self.assertTrue(supervisor.is_available("qwen"))

    def test_lock_timeout_through_failure_event_is_logged_not_raised(self) -> None:
        from codey.operations.task_phases.hooks import record_provider_failure_event
        from codey.storage.file_lock import LockTimeout

        with tempfile.TemporaryDirectory() as td:
            supervisor = ProviderSupervisor(td, clock=lambda: 100.0)
            ledger: list = []
            self_repair = mock.Mock()
            with mock.patch(
                "codey.storage.file_lock.with_file_lock",
                side_effect=LockTimeout("busy"),
            ):
                # A lock that cannot be acquired is a storage fault like any
                # other: the hook must not propagate it into the run.
                record_provider_failure_event(
                    lambda pid, failure: ledger.append((pid, failure)),
                    mock.Mock(),
                    supervisor,
                    self_repair,
                    "qwen",
                    failure("transient"),
                )
            self.assertEqual(len(ledger), 1)
            self.assertNotEqual(supervisor.last_save_error, "")
            self_repair.maybe_enqueue.assert_not_called()

    def test_no_change_update_writes_nothing(self) -> None:
        from codey.providers import supervisor as supervisor_module

        with tempfile.TemporaryDirectory() as td:
            supervisor = ProviderSupervisor(td, clock=lambda: 100.0)
            supervisor.record_success("qwen")
            with mock.patch.object(
                supervisor_module, "write_json_atomic",
            ) as write:
                # Unknown and healthy selections change nothing: zero writes.
                self.assertEqual(
                    supervisor.prepare_user_selected("ghost").state, "unknown",
                )
                self.assertEqual(
                    supervisor.prepare_user_selected("qwen").state, "healthy",
                )
                write.assert_not_called()

    def test_state_changing_updates_write_once(self) -> None:
        from codey.providers import supervisor as supervisor_module
        from codey.providers.supervisor import STATE_AUTH_REQUIRED, STATE_DEGRADED

        now = [100.0]
        with tempfile.TemporaryDirectory() as td:
            supervisor = ProviderSupervisor(td, clock=lambda: now[0])
            supervisor.record_failure(
                "qwen", failure("authentication_required"),
            )
            self.assertEqual(supervisor.get("qwen").state, STATE_AUTH_REQUIRED)
            with mock.patch.object(
                supervisor_module, "write_json_atomic",
            ) as write:
                # auth_required -> degraded persists exactly once.
                self.assertEqual(
                    supervisor.prepare_user_selected("qwen").state, STATE_DEGRADED,
                )
                self.assertEqual(write.call_count, 1)

            supervisor.record_failure("qwen", failure("rate_limited"))
            now[0] = 500.0
            with mock.patch.object(
                supervisor_module, "write_json_atomic",
            ) as write:
                # A no-op mutate on an expired OPEN still persists the
                # expiry transition itself exactly once.
                self.assertEqual(write.call_count, 0)
                self.assertEqual(
                    supervisor.prepare_user_selected("qwen").state, STATE_DEGRADED,
                )
                self.assertEqual(write.call_count, 1)

    def test_store_full_refuses_new_record_and_keeps_disk(self) -> None:
        from codey.providers import supervisor as supervisor_module

        with tempfile.TemporaryDirectory() as td:
            supervisor = ProviderSupervisor(td, clock=lambda: 100.0)
            for index in range(supervisor_module.MAX_PROVIDERS):
                supervisor.record_success(f"p{index:02d}")
            manifest = Path(td) / "provider-health.json"
            before = manifest.read_text(encoding="utf-8")
            with self.assertRaises(supervisor_module.HealthStoreError):
                supervisor.record_success("zz")
            # Explicit refusal, disk untouched: returned-healthy would be a lie.
            self.assertEqual(manifest.read_text(encoding="utf-8"), before)
            self.assertEqual(supervisor.get("zz").state, "unknown")
            reloaded = ProviderSupervisor(td, clock=lambda: 100.0)
            self.assertEqual(len(reloaded._health), supervisor_module.MAX_PROVIDERS)


class HealthFailureFanoutTests(unittest.TestCase):
    def test_failure_event_survives_health_store_outage(self) -> None:
        from codey.operations.task_phases.hooks import record_provider_failure_event
        from codey.providers.supervisor import HealthStoreError

        ledger: list = []
        supervisor = mock.Mock()
        supervisor.record_failure.side_effect = HealthStoreError("disk full")
        self_repair = mock.Mock()
        # Must not raise: the failure is ledgered, repair is skipped without
        # durable health, and failover cleanup proceeds after this returns.
        record_provider_failure_event(
            lambda pid, failure: ledger.append((pid, failure)),
            mock.Mock(),
            supervisor,
            self_repair,
            "qwen",
            failure("transient"),
        )
        self.assertEqual(len(ledger), 1)
        self_repair.maybe_enqueue.assert_not_called()

    def test_success_event_survives_health_store_outage(self) -> None:
        from codey.operations.task_phases.hooks import record_provider_success_event
        from codey.providers.supervisor import HealthStoreError

        supervisor = mock.Mock()
        supervisor.record_success.side_effect = HealthStoreError("disk full")
        record_provider_success_event(supervisor, "qwen")
        record_provider_success_event(None, "qwen")


if __name__ == "__main__":
    unittest.main()
