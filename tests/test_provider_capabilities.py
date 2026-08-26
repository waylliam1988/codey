from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace

from codey.providers.capabilities import (
    FIT_AVOID,
    FIT_OK,
    PROVIDER_CAPABILITIES,
    ProviderCapability,
    capability_for,
    rank_providers,
)
from codey.providers.diagnostics import FAILURE_KINDS, ProviderFailure
from codey.providers.supervisor import ProviderSupervisor
from codey.providers.registry import PROVIDER_LABELS


def _failure(kind: str = "response_missing") -> ProviderFailure:
    return ProviderFailure("web", "send", "", "", "missing", "now", kind)


class ProviderCapabilityTests(unittest.TestCase):
    def test_known_provider_registry_has_static_capabilities(self) -> None:
        self.assertEqual(set(PROVIDER_LABELS), set(PROVIDER_CAPABILITIES))
        for provider_id in PROVIDER_LABELS:
            with self.subTest(provider=provider_id):
                capability = capability_for(provider_id)
                self.assertEqual(capability.provider_id, provider_id)
                self.assertGreater(capability.context_budget_hint, 0)

    def test_failure_families_are_known_provider_failure_kinds(self) -> None:
        for provider_id, capability in PROVIDER_CAPABILITIES.items():
            with self.subTest(provider=provider_id):
                self.assertLessEqual(set(capability.failure_families), FAILURE_KINDS)

    def test_unknown_provider_uses_default_without_raising(self) -> None:
        capability = capability_for("future_provider")

        self.assertEqual(capability.provider_id, "future_provider")
        self.assertEqual(capability.coding_fit, FIT_OK)
        self.assertEqual(capability.research_fit, FIT_OK)

    def test_rank_preserves_input_order_for_ties(self) -> None:
        self.assertEqual(
            rank_providers(("glm", "deepseek", "stepfun"), "chat"),
            ("glm", "deepseek", "stepfun"),
        )

    def test_research_mode_pushes_avoid_fit_later_without_disabling_it(self) -> None:
        self.assertEqual(
            rank_providers(("mimo", "stepfun", "deepseek"), "research"),
            ("stepfun", "deepseek", "mimo"),
        )
        self.assertEqual(
            rank_providers(("mimo", "stepfun", "deepseek"), "hybrid"),
            ("stepfun", "deepseek", "mimo"),
        )
        self.assertEqual(rank_providers(("mimo",), "research"), ("mimo",))

    def test_project_mode_uses_coding_fit(self) -> None:
        avoided = replace(
            capability_for("future_a"),
            provider_id="future_a",
            coding_fit=FIT_AVOID,
            research_fit=FIT_OK,
        )
        neutral = replace(
            capability_for("future_b"),
            provider_id="future_b",
            coding_fit=FIT_OK,
            research_fit=FIT_OK,
        )

        with _temporary_capabilities({"future_a": avoided, "future_b": neutral}):
            self.assertEqual(
                rank_providers(("future_a", "future_b"), "project"),
                ("future_b", "future_a"),
            )
            self.assertEqual(
                rank_providers(("future_a", "future_b"), "research"),
                ("future_a", "future_b"),
            )

    def test_review_mode_uses_review_fit(self) -> None:
        avoided = replace(
            capability_for("review_a"),
            provider_id="review_a",
            review_fit=FIT_AVOID,
        )
        neutral = replace(
            capability_for("review_b"),
            provider_id="review_b",
            review_fit=FIT_OK,
        )

        with _temporary_capabilities({"review_a": avoided, "review_b": neutral}):
            self.assertEqual(
                rank_providers(("review_a", "review_b"), "review"),
                ("review_b", "review_a"),
            )

    def test_preferred_and_excluded_have_clear_priority(self) -> None:
        self.assertEqual(
            rank_providers(
                ("mimo", "stepfun", "deepseek"),
                "research",
                preferred="mimo",
            ),
            ("mimo", "stepfun", "deepseek"),
        )
        self.assertEqual(
            rank_providers(
                ("mimo", "stepfun", "deepseek"),
                "research",
                preferred="mimo",
                excluded=("mimo", "stepfun"),
            ),
            ("deepseek",),
        )
        self.assertEqual(
            rank_providers(("stepfun",), "research", preferred="future"),
            ("stepfun",),
        )

    def test_runtime_health_does_not_mutate_static_capability(self) -> None:
        before = capability_for("qwen")
        with tempfile.TemporaryDirectory() as td:
            supervisor = ProviderSupervisor(td)
            supervisor.record_failure("qwen", _failure())

        self.assertEqual(capability_for("qwen"), before)


class _temporary_capabilities:
    def __init__(self, values: dict[str, ProviderCapability]) -> None:
        self.values = values
        self.previous: dict[str, ProviderCapability | None] = {}

    def __enter__(self) -> None:
        for key, value in self.values.items():
            self.previous[key] = PROVIDER_CAPABILITIES.get(key)
            PROVIDER_CAPABILITIES[key] = value

    def __exit__(self, *_exc: object) -> None:
        for key, value in self.previous.items():
            if value is None:
                PROVIDER_CAPABILITIES.pop(key, None)
            else:
                PROVIDER_CAPABILITIES[key] = value


if __name__ == "__main__":
    unittest.main()
