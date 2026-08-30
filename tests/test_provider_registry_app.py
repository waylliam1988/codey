from __future__ import annotations

import unittest

from codey.app.provider_registry import ProviderRegistry
from codey.providers.diagnostics import FAILURE_RATE_LIMITED, ProviderFailure


class ProviderRegistryTests(unittest.TestCase):
    def test_failover_order_prefers_open_tabs(self) -> None:
        registry = ProviderRegistry()

        order = registry.failover_order(lambda: {"qwen": True, "glm": True})

        self.assertEqual(order, ("qwen", "glm", "deepseek", "mimo", "stepfun"))

    def test_session_tracking_and_forget_session(self) -> None:
        registry = ProviderRegistry()
        registry.set_session("deepseek", "s1")
        registry.set_session("qwen", "s2")

        self.assertFalse(registry.session_changed("deepseek", "s1"))
        self.assertTrue(registry.session_changed("deepseek", "s2"))

        registry.forget_session("s1")

        self.assertNotIn("deepseek", registry.sessions)
        self.assertEqual(registry.sessions["qwen"], "s2")

    def test_self_repair_candidates_skip_broken_and_unavailable(self) -> None:
        registry = ProviderRegistry()
        registry.supervisor.record_failure(
            "qwen",
            ProviderFailure(
                model="qwen",
                action="send",
                url="",
                title="",
                message="timeout",
                time="2026-08-30T00:00:00Z",
                kind=FAILURE_RATE_LIMITED,
            )
        )

        candidates = registry.self_repair_candidates(
            "deepseek",
            ordered=("deepseek", "qwen", "stepfun"),
        )

        self.assertEqual(candidates, ("stepfun",))


if __name__ == "__main__":
    unittest.main()
