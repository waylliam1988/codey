from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from codey import cancellation
from codey.provider_diagnostics import FAILURE_READINESS_STALE, ProviderFailure
from codey.provider_supervisor import (
    STATE_AUTH_REQUIRED,
    STATE_DEGRADED,
    STATE_HEALTHY,
    STATE_OPEN,
    ProviderSupervisor,
    run_half_open_canary,
)


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
                    "codey.provider_supervisor.start_deadline",
                    return_value=123.0,
                ),
                mock.patch(
                    "codey.provider_supervisor.remaining",
                    side_effect=[30.0, 5.0],
                ) as remaining_budget,
                mock.patch("codey.provider_supervisor.cancellation.deadline_scope") as scope,
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


if __name__ == "__main__":
    unittest.main()
