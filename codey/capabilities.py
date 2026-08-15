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
        ),
        CapabilitySpec(
            id="changes_presenter",
            provides=("diff_presentation",),
            ui_surface=("changes_drawer",),
            durable_state=("change_snapshots",),
            owner_module="codey.changes",
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
        ),
        CapabilitySpec(
            id="policy_guard",
            provides=("permission_profile_boundary",),
            owner_module="codey.permission_profiles",
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
            consumes=("policy_guard", "prompt_envelope", "run_trace"),
            model_visible=True,
            requires_policy=True,
            ui_surface=("research_drawer",),
            durable_state=("research_notes", "research_provenance"),
            permission_profiles=("research",),
            owner_module="codey.research.runner",
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
    "KNOWN_DURABLE_STATES",
    "KNOWN_UI_SURFACES",
    "builtin_capability_registry",
]
