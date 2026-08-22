"""Read-only registry of Codey's built-in capability boundaries.

This module is metadata only. It does not load plugins, import runtime modules,
dispatch work, or influence provider/router/permission decisions.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass

from codey.permission_profiles import PERMISSION_PROFILES


KNOWN_UI_SURFACES = frozenset({
    "changes_drawer",
    "research_drawer",
    "local_context_drawer",
    "chat_stream",
    "composer",
    "none",
})
KNOWN_DURABLE_STATES = frozenset({
    "run_ledger",
    "run_trace",
    "project_facts",
    "work_checkpoints",
    "research_notes",
    "research_provenance",
    "local_context",
    "provider_health",
    "provider_controls",
    "managed_outputs",
    "change_snapshots",
    "research_evidence_ledger",
})
# Dedicated RunTrace manifest sections a capability's projection may own.
KNOWN_TRACE_SECTIONS = frozenset({
    "prompt_sections",
    "local_context_refs",
    "research_note_ids",
    "research_source_refs",
    "research_records",
    "research_evidence_ledgers",
    "research_proof_reviews",
    "research_plans",
    "research_pipeline_runs",
    "research_connector_errors",
    "research_done_compilations",
    "analysis_runs",
    "artifact_refs",
    "reproducibility_capsules",
    "research_review_findings",
    "research_planner_gaps",
    "policy_decisions",
})
# Stable context source keys admitted through the shared ContextSource contract.
KNOWN_CONTEXT_SOURCES = frozenset({
    "ghost_directive",
    "ghost_continuity",
    "project_instructions",
    "verified_facts",
    "research_brief",
    "project_map",
    "project_config_warnings",
    "work_checkpoint",
    "initial_listing",
    "coding_current_context",
})
MODEL_VISIBLE_REQUIRED_CONSUMES = frozenset(("prompt_envelope", "run_trace"))
POLICY_REQUIRED_CONSUMES = frozenset(("policy_guard",))


@dataclass(frozen=True)
class CapabilitySpec:
    id: str
    provides: tuple[str, ...]
    consumes: tuple[str, ...] = ()
    model_visible: bool = False
    requires_policy: bool = False
    ui_surface: tuple[str, ...] = ()
    durable_state: tuple[str, ...] = ()
    permission_profiles: tuple[str, ...] = ()
    owner_module: str = ""
    third_party: bool = False
    can_override_user_choice: bool = False
    trace_sections: tuple[str, ...] = ()
    context_sources: tuple[str, ...] = ()
    evidence_producer: bool = False
    enabled_by_default: bool = True

    def to_jsonable(self) -> dict[str, object]:
        return {
            "id": self.id,
            "provides": list(self.provides),
            "consumes": list(self.consumes),
            "model_visible": bool(self.model_visible),
            "requires_policy": bool(self.requires_policy),
            "ui_surface": list(self.ui_surface),
            "durable_state": list(self.durable_state),
            "permission_profiles": list(self.permission_profiles),
            "owner_module": self.owner_module,
            "third_party": bool(self.third_party),
            "can_override_user_choice": bool(self.can_override_user_choice),
            "trace_sections": list(self.trace_sections),
            "context_sources": list(self.context_sources),
            "evidence_producer": bool(self.evidence_producer),
            "enabled_by_default": bool(self.enabled_by_default),
        }


@dataclass(frozen=True)
class CapabilityRegistry:
    specs: tuple[CapabilitySpec, ...]

    def __init__(self, specs: Iterable[CapabilitySpec]) -> None:
        ordered = tuple(sorted(specs, key=lambda item: item.id))
        object.__setattr__(self, "specs", ordered)
        self.validate()

    def all(self) -> tuple[CapabilitySpec, ...]:
        return self.specs

    def ids(self) -> tuple[str, ...]:
        return tuple(spec.id for spec in self.specs)

    def get(self, capability_id: str) -> CapabilitySpec:
        normalized = _identifier(capability_id)
        for spec in self.specs:
            if spec.id == normalized:
                return spec
        raise KeyError(f"unknown capability: {capability_id}")

    def to_jsonable(self) -> list[dict[str, object]]:
        return [spec.to_jsonable() for spec in self.specs]

    def fingerprint(self) -> str:
        payload = json.dumps(
            self.to_jsonable(),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def validate(self) -> None:
        ids = self.ids()
        if len(set(ids)) != len(ids):
            raise ValueError("capability ids must be unique")
        known_ids = set(ids)
        for spec in self.specs:
            _validate_identifier(spec.id, "capability id")
            if not spec.provides:
                raise ValueError(f"capability {spec.id} must provide at least one boundary")
            for value in spec.provides:
                _validate_identifier(value, f"{spec.id}.provides")
            for value in spec.consumes:
                _validate_identifier(value, f"{spec.id}.consumes")
                if value not in known_ids:
                    raise ValueError(f"capability {spec.id} consumes unknown capability {value}")
            unknown_surfaces = set(spec.ui_surface) - KNOWN_UI_SURFACES
            if unknown_surfaces:
                raise ValueError(f"capability {spec.id} declares unknown UI surface")
            unknown_states = set(spec.durable_state) - KNOWN_DURABLE_STATES
            if unknown_states:
                raise ValueError(f"capability {spec.id} declares unknown durable state")
            unknown_sections = set(spec.trace_sections) - KNOWN_TRACE_SECTIONS
            if unknown_sections:
                raise ValueError(f"capability {spec.id} declares unknown trace section")
            unknown_sources = set(spec.context_sources) - KNOWN_CONTEXT_SOURCES
            if unknown_sources:
                raise ValueError(f"capability {spec.id} declares unknown context source")
            unknown_profiles = set(spec.permission_profiles) - set(PERMISSION_PROFILES)
            if unknown_profiles:
                raise ValueError(f"capability {spec.id} declares unknown permission profile")
            if "none" in spec.ui_surface and len(spec.ui_surface) > 1:
                raise ValueError(f"capability {spec.id} mixes none with concrete UI surfaces")
            if spec.model_visible and not MODEL_VISIBLE_REQUIRED_CONSUMES.issubset(spec.consumes):
                raise ValueError(
                    f"model-visible capability {spec.id} must consume prompt_envelope and run_trace"
                )
            if spec.requires_policy and not POLICY_REQUIRED_CONSUMES.issubset(spec.consumes):
                raise ValueError(f"policy-bound capability {spec.id} must consume policy_guard")
            if spec.third_party:
                raise ValueError(f"capability {spec.id} may not be third-party in v1")
            if spec.can_override_user_choice:
                raise ValueError(f"capability {spec.id} may not override user choices")


def builtin_capability_registry() -> CapabilityRegistry:
    return CapabilityRegistry((
        CapabilitySpec(
            id="agent_runner",
            provides=("coding_agent_loop", "writer_prompt_boundary"),
            consumes=("local_context", "policy_guard", "prompt_envelope", "run_trace", "tool_runtime"),
            model_visible=True,
            requires_policy=True,
            permission_profiles=("coding_writer",),
            owner_module="codey.agent",
            trace_sections=("prompt_sections",),
            context_sources=(
                "project_instructions",
                "verified_facts",
                "research_brief",
                "project_map",
                "project_config_warnings",
                "work_checkpoint",
                "initial_listing",
                "coding_current_context",
            ),
        ),
        CapabilitySpec(
            id="builtin_profiles",
            provides=("builtin_profile_catalog",),
            owner_module="codey.builtin_profiles",
        ),
        CapabilitySpec(
            id="changes_presenter",
            provides=("diff_presentation",),
            ui_surface=("changes_drawer",),
            durable_state=("change_snapshots",),
            owner_module="codey.changes",
        ),
        CapabilitySpec(
            id="chat_runner",
            provides=("chat_prompt_boundary",),
            consumes=("prompt_envelope", "run_trace"),
            model_visible=True,
            owner_module="codey.task_runner",
            trace_sections=("prompt_sections",),
        ),
        CapabilitySpec(
            id="consensus_advisors",
            provides=("multi_advisor_consultation",),
            consumes=("prompt_envelope", "run_trace"),
            model_visible=True,
            owner_module="codey.consensus",
            trace_sections=("prompt_sections",),
        ),
        CapabilitySpec(
            id="context_epoch",
            provides=("safe_provider_turn_boundary", "context_admission_projection"),
            consumes=("prompt_envelope", "run_trace"),
            trace_sections=("prompt_sections",),
            owner_module="codey.context_epoch",
        ),
        CapabilitySpec(
            id="local_context",
            provides=("bounded_local_context", "ghost_continuity_context"),
            consumes=("policy_guard", "prompt_envelope", "run_trace"),
            model_visible=True,
            requires_policy=True,
            ui_surface=("local_context_drawer",),
            durable_state=("local_context",),
            owner_module="codey.ghost",
            trace_sections=("local_context_refs",),
            context_sources=("ghost_directive", "ghost_continuity"),
        ),
        CapabilitySpec(
            id="policy_guard",
            provides=("permission_profile_boundary", "action_policy_boundary"),
            owner_module="codey.action_policy",
            trace_sections=("policy_decisions",),
        ),
        CapabilitySpec(
            id="prompt_envelope",
            provides=("model_visible_section_metadata", "fail_open_trace_sink"),
            owner_module="codey.prompt_envelope",
        ),
        CapabilitySpec(
            id="provider_capability_registry",
            provides=("provider_fit_hints", "fallback_ordering_hints"),
            owner_module="codey.provider_capabilities",
        ),
        CapabilitySpec(
            id="provider_factory",
            provides=("provider_connection", "provider_session"),
            consumes=("policy_guard", "provider_capability_registry"),
            requires_policy=True,
            ui_surface=("composer",),
            durable_state=("provider_controls", "provider_health"),
            owner_module="codey.providers.registry",
        ),
        CapabilitySpec(
            id="research_runner",
            provides=("research_controller_loop", "research_prompt_boundary"),
            consumes=("policy_guard", "prompt_envelope", "research_connector_search", "run_trace"),
            model_visible=True,
            requires_policy=True,
            ui_surface=("research_drawer",),
            durable_state=("research_notes", "research_provenance"),
            permission_profiles=("research",),
            owner_module="codey.research.runner",
        ),
        CapabilitySpec(
            id="research_source_connectors",
            provides=("source_connector_registry", "source_hit_contract"),
            consumes=("policy_guard",),
            requires_policy=True,
            owner_module="codey.research.source_connectors",
        ),
        CapabilitySpec(
            id="research_connector_search",
            provides=("connector_aware_search_provider",),
            consumes=("policy_guard", "research_source_connectors"),
            requires_policy=True,
            owner_module="codey.research.connector_search",
        ),
        CapabilitySpec(
            id="research_object_model",
            provides=("research_object_projection", "claim_evidence_projection"),
            consumes=("research_runner", "run_trace"),
            durable_state=("run_trace",),
            evidence_producer=True,
            trace_sections=("research_records",),
            owner_module="codey.research.object_model",
        ),
        CapabilitySpec(
            id="research_evidence_ledger",
            provides=("durable_evidence_read_model", "evidence_locator_index"),
            consumes=("research_object_model", "run_trace"),
            durable_state=("research_evidence_ledger", "run_trace"),
            trace_sections=("research_evidence_ledgers",),
            owner_module="codey.research.evidence_ledger",
        ),
        CapabilitySpec(
            id="research_evidence_runtime",
            provides=("runtime_ref_validation", "evidence_snapshot_projection"),
            consumes=("research_object_model",),
            owner_module="codey.research.evidence_runtime",
        ),
        CapabilitySpec(
            id="research_review_finding",
            provides=("review_finding_lifecycle", "planner_gap_projection"),
            consumes=("research_evidence_runtime", "run_trace"),
            durable_state=("run_trace",),
            evidence_producer=True,
            trace_sections=("research_review_findings", "research_planner_gaps"),
            owner_module="codey.research.review_finding",
        ),
        CapabilitySpec(
            id="research_proof_quality",
            provides=("research_completion_gate", "planner_signals_v0"),
            consumes=("research_object_model", "research_evidence_ledger", "run_trace"),
            durable_state=("run_trace",),
            trace_sections=("research_proof_reviews",),
            owner_module="codey.research.proof_quality",
        ),
        CapabilitySpec(
            id="research_query_planner",
            provides=("research_plan_dry_run",),
            consumes=("research_proof_quality", "research_source_connectors", "run_trace"),
            durable_state=("run_trace",),
            trace_sections=("research_plans",),
            owner_module="codey.research.query_planner",
        ),
        CapabilitySpec(
            id="review_runner",
            provides=("review_prompt_boundary", "bounded_diff_review"),
            consumes=("policy_guard", "prompt_envelope", "run_trace"),
            model_visible=True,
            requires_policy=True,
            permission_profiles=("reviewer",),
            owner_module="codey.server",
        ),
        CapabilitySpec(
            id="run_ledger",
            provides=("task_fact_ledger", "receipt_projection"),
            durable_state=("run_ledger",),
            owner_module="codey.run_ledger",
        ),
        CapabilitySpec(
            id="run_details",
            provides=("run_details_projection",),
            consumes=("run_ledger", "run_trace"),
            ui_surface=("chat_stream",),
            owner_module="codey.run_details",
        ),
        CapabilitySpec(
            id="run_trace",
            provides=("bounded_run_manifest", "prompt_section_digest_audit"),
            durable_state=("run_trace",),
            owner_module="codey.run_trace",
        ),
        CapabilitySpec(
            id="tool_runtime",
            provides=("local_tool_execution", "tool_result_text"),
            consumes=("policy_guard", "prompt_envelope", "run_trace"),
            model_visible=True,
            requires_policy=True,
            permission_profiles=("coding_writer", "planning_readonly"),
            durable_state=("managed_outputs",),
            owner_module="codey.tool_runtime",
        ),
    ))


def _validate_identifier(value: object, label: str) -> None:
    text = str(value or "").strip()
    if not text or text != _identifier(text):
        raise ValueError(f"{label} must be snake_case")


def _identifier(value: object) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if text.startswith("_") or text.endswith("_"):
        return ""
    if "__" in text:
        return ""
    return text if text.replace("_", "").isalnum() and not text[0].isdigit() else ""


__all__ = [
    "CapabilityRegistry",
    "CapabilitySpec",
    "KNOWN_CONTEXT_SOURCES",
    "KNOWN_DURABLE_STATES",
    "KNOWN_TRACE_SECTIONS",
    "KNOWN_UI_SURFACES",
    "builtin_capability_registry",
]
