"""Read-only catalog of Codey's built-in default-profile boundaries.

Profiles are metadata only in v1. They do not load plugins, read user config,
dispatch work, alter provider selection, relax permissions, patch prompts, or
change UI behavior.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass

from codey.capabilities import builtin_capability_registry
from codey.permission_profiles import PERMISSION_PROFILES
from codey.provider_capabilities import PROVIDER_CAPABILITIES


BUILTIN_PROFILE_IDS = (
    "default",
    "research_heavy",
    "review_strict",
    "local_only",
    "beginner",
)
KNOWN_MODE_BIASES = frozenset({
    "chat",
    "hybrid",
    "planning",
    "project",
    "research",
    "review",
})
KNOWN_PERMISSION_PHASES = frozenset({
    "chat",
    "planning",
    "research",
    "review",
    "writer",
})
KNOWN_PROVIDER_SCOPES = frozenset({"all", *PROVIDER_CAPABILITIES})
KNOWN_FALLBACK_POSTURES = frozenset({
    "standard",
    "research_conservative",
    "review_conservative",
    "local_only",
    "beginner_conservative",
})
KNOWN_LOCAL_CONTEXT_DEFAULTS = frozenset({
    "existing_default",
    "enabled_for_new_projects",
    "disabled_for_new_projects",
})
KNOWN_UI_DETAIL_LEVELS = frozenset({
    "quiet",
    "beginner",
})
INTERNAL_UI_TERMS = frozenset({
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
})


@dataclass(frozen=True)
class BuiltinProfileSpec:
    id: str
    mode_bias: tuple[str, ...]
    enabled_capabilities: tuple[str, ...]
    permission_defaults: tuple[tuple[str, str], ...]
    provider_scope: tuple[str, ...]
    fallback_posture: str
    research_network: bool
    review_enabled: bool
    local_context_updates_default: str
    ui_detail_level: str
    display_name: str = ""
    user_description: str = ""
    can_override_user_provider: bool = False
    can_override_user_mode: bool = False
    can_relax_permissions: bool = False
    prompt_patches: tuple[str, ...] = ()

    def to_jsonable(self) -> dict[str, object]:
        return {
            "id": self.id,
            "mode_bias": list(self.mode_bias),
            "enabled_capabilities": list(self.enabled_capabilities),
            "permission_defaults": [
                [phase, profile] for phase, profile in self.permission_defaults
            ],
            "provider_scope": list(self.provider_scope),
            "fallback_posture": self.fallback_posture,
            "research_network": bool(self.research_network),
            "review_enabled": bool(self.review_enabled),
            "local_context_updates_default": self.local_context_updates_default,
            "ui_detail_level": self.ui_detail_level,
            "display_name": self.display_name,
            "user_description": self.user_description,
            "can_override_user_provider": bool(self.can_override_user_provider),
            "can_override_user_mode": bool(self.can_override_user_mode),
            "can_relax_permissions": bool(self.can_relax_permissions),
            "prompt_patches": list(self.prompt_patches),
        }


@dataclass(frozen=True)
class BuiltinProfileRegistry:
    specs: tuple[BuiltinProfileSpec, ...]

    def __init__(self, specs: Iterable[BuiltinProfileSpec]) -> None:
        ordered = tuple(sorted(specs, key=lambda item: item.id))
        object.__setattr__(self, "specs", ordered)
        self.validate()

    def all(self) -> tuple[BuiltinProfileSpec, ...]:
        return self.specs

    def ids(self) -> tuple[str, ...]:
        return tuple(spec.id for spec in self.specs)

    def get(self, profile_id: str) -> BuiltinProfileSpec:
        normalized = _identifier(profile_id)
        for spec in self.specs:
            if spec.id == normalized:
                return spec
        raise KeyError(f"unknown built-in profile: {profile_id}")

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
            raise ValueError("built-in profile ids must be unique")
        for spec in self.specs:
            _validate_identifier(spec.id, "profile id")
        if set(ids) != set(BUILTIN_PROFILE_IDS):
            raise ValueError("built-in profile set is not the v1 fixed set")
        known_capabilities = set(builtin_capability_registry().ids())
        known_profiles = set(PERMISSION_PROFILES)
        for spec in self.specs:
            if not spec.mode_bias:
                raise ValueError(f"profile {spec.id} must declare at least one mode bias")
            unknown_modes = set(spec.mode_bias) - KNOWN_MODE_BIASES
            if unknown_modes:
                raise ValueError(f"profile {spec.id} declares unknown mode bias")
            if len(set(spec.mode_bias)) != len(spec.mode_bias):
                raise ValueError(f"profile {spec.id} has duplicate mode bias entries")
            if not spec.enabled_capabilities:
                raise ValueError(f"profile {spec.id} must declare enabled capabilities")
            unknown_capabilities = set(spec.enabled_capabilities) - known_capabilities
            if unknown_capabilities:
                raise ValueError(f"profile {spec.id} declares unknown capability")
            phase_names: set[str] = set()
            for phase, profile in spec.permission_defaults:
                _validate_identifier(phase, f"{spec.id}.permission_defaults.phase")
                _validate_identifier(profile, f"{spec.id}.permission_defaults.profile")
                if phase not in KNOWN_PERMISSION_PHASES:
                    raise ValueError(f"profile {spec.id} declares unknown permission phase")
                if profile not in known_profiles:
                    raise ValueError(f"profile {spec.id} declares unknown permission profile")
                if phase in phase_names:
                    raise ValueError(f"profile {spec.id} has duplicate permission phase")
                phase_names.add(phase)
            if not spec.provider_scope:
                raise ValueError(f"profile {spec.id} must declare provider scope")
            unknown_scopes = set(spec.provider_scope) - KNOWN_PROVIDER_SCOPES
            if unknown_scopes:
                raise ValueError(f"profile {spec.id} declares unknown provider scope")
            if "all" in spec.provider_scope and len(spec.provider_scope) > 1:
                raise ValueError(f"profile {spec.id} mixes all with concrete providers")
            if spec.fallback_posture not in KNOWN_FALLBACK_POSTURES:
                raise ValueError(f"profile {spec.id} declares unknown fallback posture")
            if spec.local_context_updates_default not in KNOWN_LOCAL_CONTEXT_DEFAULTS:
                raise ValueError(f"profile {spec.id} declares unknown local context default")
            if spec.ui_detail_level not in KNOWN_UI_DETAIL_LEVELS:
                raise ValueError(f"profile {spec.id} declares unknown UI detail level")
            if spec.can_override_user_provider:
                raise ValueError(f"profile {spec.id} may not override user provider")
            if spec.can_override_user_mode:
                raise ValueError(f"profile {spec.id} may not override user mode")
            if spec.can_relax_permissions:
                raise ValueError(f"profile {spec.id} may not relax permissions")
            if spec.prompt_patches:
                raise ValueError(f"profile {spec.id} may not declare prompt patches in v1")


def builtin_profile_registry() -> BuiltinProfileRegistry:
    core_capabilities = (
        "agent_runner",
        "builtin_profiles",
        "changes_presenter",
        "local_context",
        "policy_guard",
        "prompt_envelope",
        "provider_capability_registry",
        "provider_factory",
        "run_ledger",
        "run_trace",
        "tool_runtime",
    )
    review_capabilities = (
        "builtin_profiles",
        "changes_presenter",
        "local_context",
        "policy_guard",
        "prompt_envelope",
        "provider_capability_registry",
        "provider_factory",
        "review_runner",
        "run_ledger",
        "run_trace",
    )
    research_capabilities = (
        *core_capabilities,
        "research_runner",
        "review_runner",
    )
    default_permissions = (
        ("chat", "chat"),
        ("planning", "planning_readonly"),
        ("research", "research"),
        ("review", "reviewer"),
        ("writer", "coding_writer"),
    )
    local_only_permissions = (
        ("chat", "chat"),
        ("planning", "planning_readonly"),
        ("review", "reviewer"),
        ("writer", "coding_writer"),
    )
    return BuiltinProfileRegistry((
        BuiltinProfileSpec(
            id="default",
            mode_bias=("chat", "project", "research", "review"),
            enabled_capabilities=research_capabilities,
            permission_defaults=default_permissions,
            provider_scope=("all",),
            fallback_posture="standard",
            research_network=True,
            review_enabled=True,
            local_context_updates_default="existing_default",
            ui_detail_level="quiet",
            display_name="Default",
            user_description="Balanced background defaults.",
        ),
        BuiltinProfileSpec(
            id="research_heavy",
            mode_bias=("research", "hybrid", "project", "review"),
            enabled_capabilities=research_capabilities,
            permission_defaults=default_permissions,
            provider_scope=("all",),
            fallback_posture="research_conservative",
            research_network=True,
            review_enabled=True,
            local_context_updates_default="existing_default",
            ui_detail_level="quiet",
            display_name="Research",
            user_description="Prefer Research when a task clearly needs outside evidence.",
        ),
        BuiltinProfileSpec(
            id="review_strict",
            mode_bias=("review", "planning"),
            enabled_capabilities=review_capabilities,
            permission_defaults=(
                ("chat", "chat"),
                ("planning", "planning_readonly"),
                ("review", "reviewer"),
            ),
            provider_scope=("all",),
            fallback_posture="review_conservative",
            research_network=False,
            review_enabled=True,
            local_context_updates_default="existing_default",
            ui_detail_level="quiet",
            display_name="Review",
            user_description="Prefer stricter read-only review defaults.",
        ),
        BuiltinProfileSpec(
            id="local_only",
            mode_bias=("chat", "project", "review", "planning"),
            enabled_capabilities=core_capabilities + ("review_runner",),
            permission_defaults=local_only_permissions,
            provider_scope=("local",),
            fallback_posture="local_only",
            research_network=False,
            review_enabled=True,
            local_context_updates_default="existing_default",
            ui_detail_level="quiet",
            display_name="Local",
            user_description="Prefer local model surfaces and avoid web Research.",
        ),
        BuiltinProfileSpec(
            id="beginner",
            mode_bias=("chat", "project", "research", "review"),
            enabled_capabilities=research_capabilities,
            permission_defaults=default_permissions,
            provider_scope=("all",),
            fallback_posture="beginner_conservative",
            research_network=True,
            review_enabled=True,
            local_context_updates_default="existing_default",
            ui_detail_level="beginner",
            display_name="Beginner",
            user_description="Keep explanations quiet and avoid internal implementation terms.",
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
    "BUILTIN_PROFILE_IDS",
    "BuiltinProfileRegistry",
    "BuiltinProfileSpec",
    "INTERNAL_UI_TERMS",
    "builtin_profile_registry",
]
