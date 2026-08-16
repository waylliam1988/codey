from __future__ import annotations

import json
import unittest
from dataclasses import replace

from codey.builtin_profiles import (
    BUILTIN_PROFILE_IDS,
    INTERNAL_UI_TERMS,
    BuiltinProfileRegistry,
    builtin_profile_registry,
)
from codey.capabilities import builtin_capability_registry
from codey.permission_profiles import PERMISSION_PROFILES
from codey.provider_capabilities import PROVIDER_CAPABILITIES


EXPECTED_BUILTIN_PROFILE_IDS = (
    "beginner",
    "default",
    "local_only",
    "research_heavy",
    "review_strict",
)
EXPECTED_BUILTIN_PROFILE_FINGERPRINT = (
    "5532eac31673a2b0d72197db98b2d21446a7ed6706625cdcea2b9f8b542d8e6c"
)
EXPECTED_INTERNAL_UI_TERMS = {
    "agent graph",
    "directive",
    "ghost",
    "memory",
    "planner",
    "provider",
    "router",
    "skill",
    "tool registry",
    "work queue",
    "workflow",
}


class BuiltinProfileRegistryTests(unittest.TestCase):
    def test_builtin_registry_exports_stable_sorted_metadata(self) -> None:
        registry = builtin_profile_registry()
        payload = registry.to_jsonable()

        self.assertEqual(registry.ids(), EXPECTED_BUILTIN_PROFILE_IDS)
        self.assertEqual(set(registry.ids()), set(BUILTIN_PROFILE_IDS))
        self.assertEqual(
            [item["id"] for item in payload],
            list(EXPECTED_BUILTIN_PROFILE_IDS),
        )
        json.dumps(payload, sort_keys=True)
        self.assertEqual(registry.fingerprint(), EXPECTED_BUILTIN_PROFILE_FINGERPRINT)
        self.assertEqual(registry.fingerprint(), builtin_profile_registry().fingerprint())

    def test_get_returns_builtin_profile(self) -> None:
        registry = builtin_profile_registry()

        profile = registry.get("research_heavy")

        self.assertIn("research", profile.mode_bias)
        self.assertTrue(profile.research_network)
        with self.assertRaises(KeyError):
            registry.get("missing_profile")

    def test_builtin_profiles_reference_known_capabilities_permissions_and_providers(self) -> None:
        known_capabilities = set(builtin_capability_registry().ids())
        known_permissions = set(PERMISSION_PROFILES)
        known_providers = set(PROVIDER_CAPABILITIES) | {"all"}

        for profile in builtin_profile_registry().all():
            with self.subTest(profile=profile.id):
                self.assertTrue(set(profile.enabled_capabilities).issubset(known_capabilities))
                self.assertTrue(
                    {item[1] for item in profile.permission_defaults}.issubset(
                        known_permissions,
                    )
                )
                self.assertTrue(set(profile.provider_scope).issubset(known_providers))

    def test_builtin_profiles_cannot_override_or_relax_user_boundaries(self) -> None:
        for profile in builtin_profile_registry().all():
            with self.subTest(profile=profile.id):
                self.assertFalse(profile.can_override_user_provider)
                self.assertFalse(profile.can_override_user_mode)
                self.assertFalse(profile.can_relax_permissions)
                self.assertEqual(profile.prompt_patches, ())

    def test_local_only_does_not_enable_network_research(self) -> None:
        profile = builtin_profile_registry().get("local_only")
        permission_defaults = dict(profile.permission_defaults)

        self.assertEqual(profile.provider_scope, ("local",))
        self.assertFalse(profile.research_network)
        self.assertNotIn("research", profile.mode_bias)
        self.assertNotIn("research", permission_defaults)
        self.assertNotIn("research", permission_defaults.values())

    def test_review_strict_does_not_declare_writer_write_default(self) -> None:
        profile = builtin_profile_registry().get("review_strict")
        permission_defaults = dict(profile.permission_defaults)

        self.assertEqual(permission_defaults["review"], "reviewer")
        self.assertEqual(permission_defaults["planning"], "planning_readonly")
        self.assertNotIn("writer", permission_defaults)
        self.assertNotIn("coding_writer", permission_defaults.values())

    def test_beginner_copy_does_not_expose_internal_terms(self) -> None:
        profile = builtin_profile_registry().get("beginner")
        visible_text = f"{profile.display_name} {profile.user_description}".casefold()

        self.assertEqual(profile.ui_detail_level, "beginner")
        for term in INTERNAL_UI_TERMS:
            self.assertNotIn(term, visible_text)

    def test_internal_ui_terms_cover_principle_and_design_language(self) -> None:
        self.assertTrue(EXPECTED_INTERNAL_UI_TERMS.issubset(INTERNAL_UI_TERMS))

    def test_state_and_task_runner_carry_registry_without_dispatch(self) -> None:
        from codey import server
        from codey.task_runner import TaskRunner

        state = server.State()
        registry = state.builtin_profiles
        runner = TaskRunner(
            state,
            agent_run=lambda **_kwargs: None,
            collect_changes=lambda *_args, **_kwargs: {},
            run_review=lambda **_kwargs: None,
            capture_provider_failure=lambda *_args, **_kwargs: None,
            builtin_profiles=registry,
        )

        self.assertEqual(registry.ids(), EXPECTED_BUILTIN_PROFILE_IDS)
        self.assertIs(runner.builtin_profiles, registry)

    def test_validation_rejects_unknown_capability_permission_and_provider(self) -> None:
        specs = list(builtin_profile_registry().all())

        with self.assertRaisesRegex(ValueError, "unknown capability"):
            BuiltinProfileRegistry((
                replace(specs[0], enabled_capabilities=("missing_capability",)),
                *specs[1:],
            ))
        with self.assertRaisesRegex(ValueError, "unknown permission profile"):
            BuiltinProfileRegistry((
                replace(specs[0], permission_defaults=(("chat", "missing_profile"),)),
                *specs[1:],
            ))
        with self.assertRaisesRegex(ValueError, "unknown provider scope"):
            BuiltinProfileRegistry((
                replace(specs[0], provider_scope=("missing_provider",)),
                *specs[1:],
            ))

    def test_validation_rejects_override_relax_and_prompt_patch_flags(self) -> None:
        specs = list(builtin_profile_registry().all())

        with self.assertRaisesRegex(ValueError, "override user provider"):
            BuiltinProfileRegistry((
                replace(specs[0], can_override_user_provider=True),
                *specs[1:],
            ))
        with self.assertRaisesRegex(ValueError, "override user mode"):
            BuiltinProfileRegistry((
                replace(specs[0], can_override_user_mode=True),
                *specs[1:],
            ))
        with self.assertRaisesRegex(ValueError, "relax permissions"):
            BuiltinProfileRegistry((
                replace(specs[0], can_relax_permissions=True),
                *specs[1:],
            ))
        with self.assertRaisesRegex(ValueError, "prompt patches"):
            BuiltinProfileRegistry((
                replace(specs[0], prompt_patches=("extra prompt",)),
                *specs[1:],
            ))

    def test_validation_rejects_non_v1_profile_set(self) -> None:
        specs = builtin_profile_registry().all()

        with self.assertRaisesRegex(ValueError, "v1 fixed set"):
            BuiltinProfileRegistry(specs[:-1])

    def test_validation_rejects_empty_and_non_snake_case_ids(self) -> None:
        specs = list(builtin_profile_registry().all())

        with self.assertRaisesRegex(ValueError, "snake_case"):
            BuiltinProfileRegistry((replace(specs[0], id=""), *specs[1:]))
        with self.assertRaisesRegex(ValueError, "snake_case"):
            BuiltinProfileRegistry((replace(specs[0], id="BadProfile"), *specs[1:]))


if __name__ == "__main__":
    unittest.main()
