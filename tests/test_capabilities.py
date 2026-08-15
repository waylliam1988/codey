from __future__ import annotations

import json
import unittest

from codey.capabilities import (
    CapabilityRegistry,
    CapabilitySpec,
    KNOWN_DURABLE_STATES,
    KNOWN_UI_SURFACES,
    builtin_capability_registry,
)
from codey.permission_profiles import PERMISSION_PROFILES


EXPECTED_BUILTIN_IDS = (
    "agent_runner",
    "changes_presenter",
    "local_context",
    "policy_guard",
    "prompt_envelope",
    "provider_capability_registry",
    "provider_factory",
    "research_runner",
    "review_runner",
    "run_ledger",
    "run_trace",
    "tool_runtime",
)
EXPECTED_BUILTIN_FINGERPRINT = (
    "4277987fe6634e9d69a6d71055775dc055c2c3c823bdf9e258252a26b4720a58"
)


class CapabilityRegistryTests(unittest.TestCase):
    def test_builtin_registry_exports_stable_sorted_metadata(self) -> None:
        registry = builtin_capability_registry()
        payload = registry.to_jsonable()

        self.assertEqual(registry.ids(), EXPECTED_BUILTIN_IDS)
        self.assertEqual([item["id"] for item in payload], list(EXPECTED_BUILTIN_IDS))
        json.dumps(payload, sort_keys=True)
        self.assertEqual(registry.fingerprint(), EXPECTED_BUILTIN_FINGERPRINT)
        self.assertEqual(registry.fingerprint(), builtin_capability_registry().fingerprint())

    def test_get_returns_builtin_spec(self) -> None:
        registry = builtin_capability_registry()

        spec = registry.get("research_runner")

        self.assertEqual(spec.owner_module, "codey.research.runner")
        self.assertIn("research", spec.permission_profiles)
        with self.assertRaises(KeyError):
            registry.get("missing_runner")

    def test_changes_presenter_is_display_only_in_v1(self) -> None:
        registry = builtin_capability_registry()

        spec = registry.get("changes_presenter")

        self.assertEqual(spec.provides, ("diff_presentation",))
        self.assertFalse(spec.requires_policy)
        self.assertEqual(spec.consumes, ())

    def test_policy_guard_declares_action_policy_boundary(self) -> None:
        registry = builtin_capability_registry()

        spec = registry.get("policy_guard")

        self.assertEqual(
            spec.provides,
            ("permission_profile_boundary", "action_policy_boundary"),
        )
        self.assertEqual(spec.owner_module, "codey.action_policy")

    def test_state_and_task_runner_carry_registry_without_dispatch(self) -> None:
        from codey import server
        from codey.task_runner import TaskRunner

        state = server.State()
        registry = state.capabilities
        runner = TaskRunner(
            state,
            agent_run=lambda **_kwargs: None,
            collect_changes=lambda *_args, **_kwargs: {},
            run_review=lambda **_kwargs: None,
            capture_provider_failure=lambda *_args, **_kwargs: None,
            capabilities=registry,
        )

        self.assertEqual(registry.ids(), EXPECTED_BUILTIN_IDS)
        self.assertIs(runner.capabilities, registry)

    def test_builtin_capabilities_do_not_load_third_party_or_override_users(self) -> None:
        registry = builtin_capability_registry()

        self.assertTrue(all(not spec.third_party for spec in registry.all()))
        self.assertTrue(all(not spec.can_override_user_choice for spec in registry.all()))

    def test_model_visible_capabilities_consume_prompt_envelope_and_run_trace(self) -> None:
        registry = builtin_capability_registry()

        for spec in registry.all():
            if spec.model_visible:
                self.assertIn("prompt_envelope", spec.consumes)
                self.assertIn("run_trace", spec.consumes)

    def test_policy_bound_capabilities_consume_policy_guard(self) -> None:
        registry = builtin_capability_registry()

        for spec in registry.all():
            if spec.requires_policy:
                self.assertIn("policy_guard", spec.consumes)

    def test_builtin_declarations_use_known_surfaces_states_and_profiles(self) -> None:
        registry = builtin_capability_registry()

        for spec in registry.all():
            self.assertTrue(set(spec.ui_surface).issubset(KNOWN_UI_SURFACES))
            self.assertTrue(set(spec.durable_state).issubset(KNOWN_DURABLE_STATES))
            self.assertTrue(set(spec.permission_profiles).issubset(PERMISSION_PROFILES))

    def test_validation_rejects_unknown_dependency(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown capability"):
            CapabilityRegistry((
                CapabilitySpec("known", ("known_boundary",)),
                CapabilitySpec("bad", ("bad_boundary",), consumes=("missing",)),
            ))

    def test_validation_rejects_empty_identifiers(self) -> None:
        with self.assertRaisesRegex(ValueError, "snake_case"):
            CapabilityRegistry((
                CapabilitySpec("", ("known_boundary",)),
            ))

        with self.assertRaisesRegex(ValueError, "snake_case"):
            CapabilityRegistry((
                CapabilitySpec("bad", ("",)),
            ))

        with self.assertRaisesRegex(ValueError, "snake_case"):
            CapabilityRegistry((
                CapabilitySpec("known", ("known_boundary",)),
                CapabilitySpec("bad", ("bad_boundary",), consumes=("",)),
            ))

    def test_validation_rejects_unknown_permission_profile_ui_surface_and_state(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown permission profile"):
            CapabilityRegistry((
                CapabilitySpec("bad", ("bad_boundary",), permission_profiles=("admin",)),
            ))

        with self.assertRaisesRegex(ValueError, "unknown UI surface"):
            CapabilityRegistry((
                CapabilitySpec("bad", ("bad_boundary",), ui_surface=("plugin_panel",)),
            ))

        with self.assertRaisesRegex(ValueError, "unknown durable state"):
            CapabilityRegistry((
                CapabilitySpec("bad", ("bad_boundary",), durable_state=("plugin_cache",)),
            ))

    def test_validation_rejects_missing_model_visible_and_policy_edges(self) -> None:
        with self.assertRaisesRegex(ValueError, "model-visible capability"):
            CapabilityRegistry((
                CapabilitySpec("prompt_envelope", ("prompt_boundary",)),
                CapabilitySpec("run_trace", ("trace_boundary",)),
                CapabilitySpec(
                    "bad",
                    ("bad_boundary",),
                    consumes=("prompt_envelope",),
                    model_visible=True,
                ),
            ))

        with self.assertRaisesRegex(ValueError, "policy-bound capability"):
            CapabilityRegistry((
                CapabilitySpec("policy_guard", ("policy_boundary",)),
                CapabilitySpec("bad", ("bad_boundary",), requires_policy=True),
            ))

    def test_validation_rejects_third_party_and_user_override_flags(self) -> None:
        with self.assertRaisesRegex(ValueError, "third-party"):
            CapabilityRegistry((
                CapabilitySpec("bad", ("bad_boundary",), third_party=True),
            ))

        with self.assertRaisesRegex(ValueError, "override user choices"):
            CapabilityRegistry((
                CapabilitySpec("bad", ("bad_boundary",), can_override_user_choice=True),
            ))

    def test_validation_rejects_non_snake_case_ids(self) -> None:
        with self.assertRaisesRegex(ValueError, "snake_case"):
            CapabilityRegistry((
                CapabilitySpec("BadCapability", ("bad_boundary",)),
            ))


if __name__ == "__main__":
    unittest.main()
