from __future__ import annotations

import json
import unittest

from codey.capabilities import (
    CapabilityRegistry,
    CapabilitySpec,
    KNOWN_CONTEXT_SOURCES,
    KNOWN_DURABLE_STATES,
    KNOWN_TRACE_SECTIONS,
    KNOWN_UI_SURFACES,
    builtin_capability_registry,
)
from codey.permission_profiles import PERMISSION_PROFILES


EXPECTED_BUILTIN_IDS = (
    "agent_runner",
    "builtin_profiles",
    "changes_presenter",
    "chat_runner",
    "consensus_advisors",
    "context_epoch",
    "conversation_handoff",
    "local_context",
    "policy_guard",
    "prompt_envelope",
    "provider_capability_registry",
    "provider_factory",
    "research_connector_search",
    "research_evidence_ledger",
    "research_evidence_runtime",
    "research_object_model",
    "research_proof_quality",
    "research_query_planner",
    "research_review_finding",
    "research_runner",
    "research_source_connectors",
    "review_runner",
    "run_details",
    "run_ledger",
    "run_trace",
    "tool_runtime",
)
EXPECTED_BUILTIN_FINGERPRINT = (
    "b5fccad6bb5c274020978862a0c4a537242dec47bbc5d87de4d1bd149cb96eb8"
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

    def test_builtin_profiles_is_metadata_only_in_v1(self) -> None:
        registry = builtin_capability_registry()

        spec = registry.get("builtin_profiles")

        self.assertEqual(spec.provides, ("builtin_profile_catalog",))
        self.assertEqual(spec.owner_module, "codey.builtin_profiles")
        self.assertFalse(spec.model_visible)
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

    def test_run_details_is_read_only_chat_projection(self) -> None:
        registry = builtin_capability_registry()

        spec = registry.get("run_details")

        self.assertEqual(spec.provides, ("run_details_projection",))
        self.assertEqual(spec.consumes, ("run_ledger", "run_trace"))
        self.assertEqual(spec.ui_surface, ("chat_stream",))
        self.assertEqual(spec.owner_module, "codey.run_details")
        self.assertFalse(spec.model_visible)
        self.assertFalse(spec.requires_policy)
        self.assertEqual(spec.durable_state, ())

    def test_research_object_model_is_trace_only_projection(self) -> None:
        registry = builtin_capability_registry()

        spec = registry.get("research_object_model")

        self.assertEqual(
            spec.provides,
            ("research_object_projection", "claim_evidence_projection"),
        )
        self.assertEqual(spec.consumes, ("research_runner", "run_trace"))
        self.assertEqual(spec.durable_state, ("run_trace",))
        self.assertEqual(spec.owner_module, "codey.research.object_model")
        self.assertFalse(spec.model_visible)
        self.assertFalse(spec.requires_policy)
        self.assertEqual(spec.ui_surface, ())

    def test_research_evidence_ledger_is_quiet_durable_read_model(self) -> None:
        registry = builtin_capability_registry()

        spec = registry.get("research_evidence_ledger")

        self.assertEqual(
            spec.provides,
            ("durable_evidence_read_model", "evidence_locator_index"),
        )
        self.assertEqual(spec.consumes, ("research_object_model", "run_trace"))
        self.assertEqual(spec.durable_state, ("research_evidence_ledger", "run_trace"))
        self.assertEqual(spec.owner_module, "codey.research.evidence_ledger")
        self.assertFalse(spec.model_visible)
        self.assertFalse(spec.requires_policy)
        self.assertEqual(spec.ui_surface, ())

    def test_research_proof_quality_is_deterministic_completion_gate(self) -> None:
        registry = builtin_capability_registry()

        spec = registry.get("research_proof_quality")

        self.assertEqual(
            spec.provides,
            ("research_completion_gate", "planner_signals_v0"),
        )
        self.assertEqual(
            spec.consumes,
            ("research_object_model", "research_evidence_ledger", "run_trace"),
        )
        self.assertEqual(spec.durable_state, ("run_trace",))
        self.assertEqual(spec.owner_module, "codey.research.proof_quality")
        self.assertFalse(spec.model_visible)
        self.assertFalse(spec.requires_policy)
        self.assertEqual(spec.ui_surface, ())

    def test_research_source_connectors_are_policy_bound_metadata(self) -> None:
        registry = builtin_capability_registry()

        spec = registry.get("research_source_connectors")

        self.assertEqual(
            spec.provides,
            ("source_connector_registry", "source_hit_contract"),
        )
        self.assertEqual(spec.consumes, ("policy_guard",))
        self.assertEqual(spec.owner_module, "codey.research.source_connectors")
        self.assertFalse(spec.model_visible)
        self.assertTrue(spec.requires_policy)
        self.assertEqual(spec.ui_surface, ())
        self.assertEqual(spec.durable_state, ())

    def test_research_connector_search_is_policy_bound_provider_wrapper(self) -> None:
        registry = builtin_capability_registry()

        spec = registry.get("research_connector_search")

        self.assertEqual(spec.provides, ("connector_aware_search_provider",))
        self.assertEqual(spec.consumes, ("policy_guard", "research_source_connectors"))
        self.assertEqual(spec.owner_module, "codey.research.connector_search")
        self.assertFalse(spec.model_visible)
        self.assertTrue(spec.requires_policy)
        self.assertEqual(spec.ui_surface, ())
        self.assertEqual(spec.durable_state, ())

    def test_research_query_planner_is_trace_only_dry_run(self) -> None:
        registry = builtin_capability_registry()

        spec = registry.get("research_query_planner")

        self.assertEqual(spec.provides, ("research_plan_dry_run",))
        self.assertEqual(
            spec.consumes,
            ("research_proof_quality", "research_source_connectors", "run_trace"),
        )
        self.assertEqual(spec.durable_state, ("run_trace",))
        self.assertEqual(spec.owner_module, "codey.research.query_planner")
        self.assertFalse(spec.model_visible)
        self.assertFalse(spec.requires_policy)
        self.assertEqual(spec.ui_surface, ())
        self.assertEqual(spec.trace_sections, ("research_plans",))

    def test_context_epoch_declares_safe_provider_turn_boundary(self) -> None:
        registry = builtin_capability_registry()

        spec = registry.get("context_epoch")

        self.assertEqual(
            spec.provides,
            ("safe_provider_turn_boundary", "context_admission_projection"),
        )
        self.assertEqual(spec.consumes, ("prompt_envelope", "run_trace"))
        self.assertEqual(spec.trace_sections, ("prompt_sections",))
        self.assertEqual(spec.owner_module, "codey.context_epoch")
        self.assertFalse(spec.model_visible)
        self.assertFalse(spec.evidence_producer)

    def test_research_evidence_runtime_is_ref_validation_boundary(self) -> None:
        registry = builtin_capability_registry()

        spec = registry.get("research_evidence_runtime")

        self.assertEqual(
            spec.provides,
            ("runtime_ref_validation", "evidence_snapshot_projection"),
        )
        self.assertEqual(spec.consumes, ("research_object_model",))
        self.assertEqual(spec.durable_state, ())
        self.assertEqual(spec.owner_module, "codey.research.evidence_runtime")
        self.assertFalse(spec.model_visible)
        self.assertFalse(spec.evidence_producer)
        self.assertEqual(spec.trace_sections, ())

    def test_research_review_finding_declares_finding_trace_sections(self) -> None:
        registry = builtin_capability_registry()

        spec = registry.get("research_review_finding")

        self.assertEqual(
            spec.provides,
            ("review_finding_lifecycle", "planner_gap_projection"),
        )
        self.assertEqual(spec.consumes, ("research_evidence_runtime", "run_trace"))
        self.assertEqual(
            spec.trace_sections,
            ("research_review_findings", "research_planner_gaps"),
        )
        self.assertTrue(spec.evidence_producer)
        self.assertEqual(spec.durable_state, ("run_trace",))
        self.assertEqual(spec.owner_module, "codey.research.review_finding")

    def test_chat_runner_declares_chat_prompt_boundary(self) -> None:
        registry = builtin_capability_registry()

        spec = registry.get("chat_runner")

        self.assertEqual(spec.provides, ("chat_prompt_boundary",))
        self.assertEqual(spec.consumes, ("prompt_envelope", "run_trace"))
        self.assertTrue(spec.model_visible)
        self.assertFalse(spec.requires_policy)
        self.assertEqual(spec.owner_module, "codey.task_runner")
        self.assertEqual(spec.trace_sections, ("prompt_sections",))

    def test_consensus_advisors_is_model_visible_consultation_boundary(self) -> None:
        registry = builtin_capability_registry()

        spec = registry.get("consensus_advisors")

        self.assertEqual(spec.provides, ("multi_advisor_consultation",))
        self.assertEqual(spec.consumes, ("prompt_envelope", "run_trace"))
        self.assertTrue(spec.model_visible)
        self.assertFalse(spec.requires_policy)
        self.assertEqual(spec.ui_surface, ())
        self.assertEqual(spec.durable_state, ())

    def test_conversation_handoff_declares_summary_prompt_boundary(self) -> None:
        registry = builtin_capability_registry()

        spec = registry.get("conversation_handoff")

        self.assertEqual(
            spec.provides,
            ("conversation_handoff_summary_prompt_boundary",),
        )
        self.assertEqual(spec.consumes, ("prompt_envelope", "run_trace"))
        self.assertTrue(spec.model_visible)
        self.assertFalse(spec.requires_policy)
        self.assertEqual(spec.owner_module, "codey.handoff")
        self.assertEqual(spec.trace_sections, ("prompt_sections",))

    def test_local_context_owns_ghost_context_sources(self) -> None:
        registry = builtin_capability_registry()

        spec = registry.get("local_context")

        self.assertEqual(spec.context_sources, ("ghost_directive", "ghost_continuity"))

    def test_agent_runner_owns_coding_context_sources(self) -> None:
        registry = builtin_capability_registry()

        spec = registry.get("agent_runner")

        self.assertEqual(
            spec.context_sources,
            (
                "project_instructions",
                "verified_facts",
                "research_brief",
                "project_map",
                "project_config_warnings",
                "work_checkpoint",
                "initial_listing",
                "coding_current_context",
            ),
        )

    def test_builtin_capabilities_are_enabled_by_default(self) -> None:
        registry = builtin_capability_registry()

        self.assertTrue(all(spec.enabled_by_default for spec in registry.all()))
        self.assertTrue(all(
            set(spec.trace_sections).issubset(KNOWN_TRACE_SECTIONS)
            for spec in registry.all()
        ))
        self.assertTrue(all(
            set(spec.context_sources).issubset(KNOWN_CONTEXT_SOURCES)
            for spec in registry.all()
        ))

    def test_evidence_producers_are_explicit(self) -> None:
        registry = builtin_capability_registry()

        producers = tuple(
            spec.id for spec in registry.all() if spec.evidence_producer
        )

        self.assertEqual(producers, ("research_object_model", "research_review_finding"))

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

    def test_validation_rejects_unknown_trace_section_and_context_source(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown trace section"):
            CapabilityRegistry((
                CapabilitySpec("bad", ("bad_boundary",), trace_sections=("ghost_sections",)),
            ))

        with self.assertRaisesRegex(ValueError, "unknown context source"):
            CapabilityRegistry((
                CapabilitySpec("bad", ("bad_boundary",), context_sources=("mystery_source",)),
            ))

    def test_validation_rejects_non_snake_case_ids(self) -> None:
        with self.assertRaisesRegex(ValueError, "snake_case"):
            CapabilityRegistry((
                CapabilitySpec("BadCapability", ("bad_boundary",)),
            ))


if __name__ == "__main__":
    unittest.main()
