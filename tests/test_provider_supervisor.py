from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codey.provider_diagnostics import ProviderFailure
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
                "mimo", failure("submission_uncertain")
            )

            self.assertEqual(health.state, STATE_DEGRADED)
            self.assertTrue(supervisor.is_available("mimo"))

    def test_selection_is_deterministic_and_skips_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            supervisor = ProviderSupervisor(td, clock=lambda: 100.0)
            supervisor.record_failure("qwen", failure("rate_limited"))

            selected = supervisor.select(
                "qwen",
                ("deepseek", "mimo", "glm"),
                excluded=("deepseek",),
            )

            self.assertEqual(selected, "mimo")

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
            provider.send.side_effect = lambda prompt: prompt.rsplit(" ", 1)[-1]

            ok = run_half_open_canary("qwen", provider, supervisor)

            self.assertTrue(ok)
            provider.new_chat.assert_called_once_with()
            prompt = provider.send.call_args.args[0]
            self.assertIn("CODEY_CANARY_", prompt)
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


if __name__ == "__main__":
    unittest.main()
