"""Monotonic local action policy for Codey runtime boundaries.

This module is pure policy metadata and deterministic guards. It does not
execute tools, open browsers, dispatch providers, or mutate runtime state.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from codey.policies.network import DEFAULT_NETWORK_POLICY
from codey.policies.permissions import PermissionProfile, profile_for_name
from codey.policies.run_command_semantics import (
    RunCommandPolicyError,
    canonical_run_command,
)


DECISION_ALLOW = "allow"
DECISION_ASK_USER = "ask_user"
DECISION_DENY = "deny"
DECISION_ORDER = {
    DECISION_ALLOW: 0,
    DECISION_ASK_USER: 1,
    DECISION_DENY: 2,
}

MAX_DISPLAY_CHARS = 240
MAX_REASON_CHARS = 120
MAX_MANAGED_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_MANAGED_OUTPUTS_PER_RUN = 32

READ_ACTIONS = frozenset({
    "read_file",
    "list_dir",
    "search_files",
    "find_references",
})
WRITE_ACTIONS = frozenset({
    "write_file",
    "edit_file",
})
PATH_ACTIONS = READ_ACTIONS | WRITE_ACTIONS | frozenset({
    "run_command",
    "shell",
})
KNOWN_ACTIONS = PATH_ACTIONS | frozenset({
    "research_url",
    "provider_fallback",
    "managed_output",
    "local_context_action",
})
DANGEROUS_ACTIONS = WRITE_ACTIONS | frozenset({
    "run_command",
    "shell",
    "research_url",
    "provider_fallback",
    "managed_output",
    "local_context_action",
    "unknown_tool",
})


@dataclass(frozen=True)
class ActionSubject:
    kind: str
    phase: str = ""
    permission_profile: str = ""
    project: str = ""
    path: str = ""
    command: str = ""
    url: str = ""
    tool_name: str = ""
    from_provider: str = ""
    to_provider: str = ""
    byte_count: int = 0
    item_count: int = 0
    approval_available: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", _identifier(self.kind, 80))
        object.__setattr__(self, "phase", _identifier(self.phase, 80))
        object.__setattr__(self, "permission_profile", _identifier(self.permission_profile, 80))
        object.__setattr__(self, "tool_name", _identifier(self.tool_name, 80))
        object.__setattr__(self, "from_provider", _identifier(self.from_provider, 80))
        object.__setattr__(self, "to_provider", _identifier(self.to_provider, 80))
        object.__setattr__(self, "path", str(self.path or "").strip())
        object.__setattr__(self, "project", str(self.project or "").strip())
        object.__setattr__(self, "command", str(self.command or "").strip())
        object.__setattr__(self, "url", str(self.url or "").strip())
        object.__setattr__(self, "byte_count", _nonnegative_int(self.byte_count))
        object.__setattr__(self, "item_count", _nonnegative_int(self.item_count))
        object.__setattr__(self, "approval_available", bool(self.approval_available))

    @property
    def subject_ref(self) -> str:
        private = {
            "kind": self.kind,
            "phase": self.phase,
            "permission_profile": self.permission_profile,
            "project_digest": digest_text(_normcase_path(self.project)) if self.project else "",
            "path_digest": digest_text(self.path) if self.path else "",
            "command_digest": digest_text(self.command) if self.command else "",
            "url_digest": digest_text(self.url) if self.url else "",
            "tool_name": self.tool_name,
            "from_provider": self.from_provider,
            "to_provider": self.to_provider,
            "byte_count": self.byte_count,
            "item_count": self.item_count,
            "approval_available": self.approval_available,
        }
        payload = json.dumps(private, sort_keys=True, separators=(",", ":"))
        return "action:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ActionPolicyDecision:
    decision: str = DECISION_ALLOW
    guard_id: str = "default_allow"
    reason_code: str = "allowed"
    display: str = ""
    subject_ref: str = ""
    kind: str = ""
    phase: str = ""

    def __post_init__(self) -> None:
        decision = _identifier(self.decision, 40)
        if decision not in DECISION_ORDER:
            decision = DECISION_DENY
        guard_id = _identifier(self.guard_id, 80) or "unknown_guard"
        reason_code = _identifier(self.reason_code, MAX_REASON_CHARS) or "unknown"
        object.__setattr__(self, "decision", decision)
        object.__setattr__(self, "guard_id", guard_id)
        object.__setattr__(self, "reason_code", reason_code)
        object.__setattr__(self, "display", _clip_display(self.display))
        object.__setattr__(self, "subject_ref", _clip_ref(self.subject_ref))
        object.__setattr__(self, "kind", _identifier(self.kind, 80))
        object.__setattr__(self, "phase", _identifier(self.phase, 80))

    @classmethod
    def allow(
        cls,
        subject: ActionSubject,
        *,
        guard_id: str = "default_allow",
        reason_code: str = "allowed",
    ) -> "ActionPolicyDecision":
        return cls(
            DECISION_ALLOW,
            guard_id,
            reason_code,
            subject_ref=subject.subject_ref,
            kind=subject.kind,
            phase=subject.phase,
        )

    @classmethod
    def ask_user(
        cls,
        subject: ActionSubject,
        *,
        guard_id: str,
        reason_code: str,
        display: str,
    ) -> "ActionPolicyDecision":
        return cls(
            DECISION_ASK_USER,
            guard_id,
            reason_code,
            display,
            subject.subject_ref,
            subject.kind,
            subject.phase,
        )

    @classmethod
    def deny(
        cls,
        subject: ActionSubject,
        *,
        guard_id: str,
        reason_code: str,
        display: str,
    ) -> "ActionPolicyDecision":
        return cls(
            DECISION_DENY,
            guard_id,
            reason_code,
            display,
            subject.subject_ref,
            subject.kind,
            subject.phase,
        )

    def to_audit_payload(self) -> dict[str, object]:
        payload = {
            "kind": self.kind,
            "decision": self.decision,
            "guard_id": self.guard_id,
            "reason_code": self.reason_code,
            "phase": self.phase,
            "subject_ref": self.subject_ref,
        }
        if self.display:
            payload["display_digest"] = digest_text(self.display)
            payload["display_chars"] = len(self.display)
        return payload


PolicyGuard = Callable[[ActionSubject], ActionPolicyDecision | None]


@dataclass(frozen=True)
class ActionPolicyPipeline:
    guards: tuple[PolicyGuard, ...]

    def evaluate(self, subject: ActionSubject) -> ActionPolicyDecision:
        decision = ActionPolicyDecision.allow(subject)
        for guard in self.guards:
            try:
                candidate = guard(subject)
            except Exception:
                if subject.kind in DANGEROUS_ACTIONS:
                    candidate = ActionPolicyDecision.deny(
                        subject,
                        guard_id="guard_exception",
                        reason_code="guard_exception",
                        display="action denied because a policy guard failed",
                    )
                else:
                    candidate = None
            if candidate is not None:
                decision = merge_decisions(decision, candidate)
        return decision


def default_action_policy_pipeline() -> ActionPolicyPipeline:
    return ActionPolicyPipeline((
        unknown_action_guard,
        permission_profile_guard,
        workspace_path_guard,
        write_scope_guard,
        run_command_guard,
        shell_approval_guard,
        research_url_guard,
        local_context_action_guard,
        provider_fallback_guard,
        managed_output_size_guard,
    ))


def evaluate_action(subject: ActionSubject) -> ActionPolicyDecision:
    return default_action_policy_pipeline().evaluate(subject)


def merge_decisions(
    current: ActionPolicyDecision,
    candidate: ActionPolicyDecision,
) -> ActionPolicyDecision:
    current_rank = DECISION_ORDER.get(current.decision, DECISION_ORDER[DECISION_DENY])
    candidate_rank = DECISION_ORDER.get(candidate.decision, DECISION_ORDER[DECISION_DENY])
    if candidate_rank > current_rank:
        return candidate
    return current


def unknown_action_guard(subject: ActionSubject) -> ActionPolicyDecision | None:
    if subject.kind in KNOWN_ACTIONS:
        return None
    return ActionPolicyDecision.deny(
        subject,
        guard_id="unknown_action_guard",
        reason_code="unknown_action",
        display="action denied because the action kind is unknown",
    )


def permission_profile_guard(subject: ActionSubject) -> ActionPolicyDecision | None:
    if subject.kind not in PATH_ACTIONS | frozenset({"research_url", "managed_output"}):
        return None
    profile = _profile(subject)
    if profile is None:
        return ActionPolicyDecision.deny(
            subject,
            guard_id="permission_profile_guard",
            reason_code="unknown_permission_profile",
            display="action denied by unknown permission profile",
        )
    if subject.kind in READ_ACTIONS and not profile.project_read:
        return _permission_denied(subject)
    if subject.kind in WRITE_ACTIONS and not profile.project_write:
        return _permission_denied(subject)
    if subject.kind == "run_command" and "project_verify" not in profile.coding_permissions:
        return _permission_denied(subject)
    if subject.kind == "shell" and not profile.can_request_shell:
        return _permission_denied(subject)
    if subject.kind == "research_url" and "open_url" not in profile.research_tools:
        return _permission_denied(subject)
    if subject.kind == "managed_output" and "project_verify" not in profile.coding_permissions:
        return _permission_denied(subject)
    return None


def workspace_path_guard(subject: ActionSubject) -> ActionPolicyDecision | None:
    if subject.kind not in PATH_ACTIONS:
        return None
    if not subject.project:
        return ActionPolicyDecision.deny(
            subject,
            guard_id="workspace_path_guard",
            reason_code="project_required",
            display="project required for local workspace action",
        )
    try:
        root = Path(subject.project).expanduser().resolve()
        path = (root / (subject.path or ".")).resolve()
        if root not in path.parents and path != root:
            return ActionPolicyDecision.deny(
                subject,
                guard_id="workspace_path_guard",
                reason_code="workspace_escape",
                display=f"path escapes project root: {subject.path}",
            )
    except (OSError, RuntimeError, ValueError) as exc:
        return ActionPolicyDecision.deny(
            subject,
            guard_id="workspace_path_guard",
            reason_code="path_resolution_failed",
            display=str(exc) or "path resolution failed",
        )
    return None


def write_scope_guard(subject: ActionSubject) -> ActionPolicyDecision | None:
    if subject.kind not in WRITE_ACTIONS:
        return None
    if not subject.path or subject.path == ".":
        return ActionPolicyDecision.deny(
            subject,
            guard_id="write_scope_guard",
            reason_code="write_path_required",
            display="path required for file write",
        )
    return None


def run_command_guard(subject: ActionSubject) -> ActionPolicyDecision | None:
    if subject.kind != "run_command":
        return None
    command = subject.command.strip()
    if not command:
        return ActionPolicyDecision.deny(
            subject,
            guard_id="run_command_guard",
            reason_code="command_required",
            display="command required",
        )
    try:
        canonical_run_command(subject.project, subject.path or ".", command)
    except RunCommandPolicyError as exc:
        return ActionPolicyDecision.deny(
            subject,
            guard_id="run_command_guard",
            reason_code=exc.reason_code,
            display=exc.display,
        )
    return None


def shell_approval_guard(subject: ActionSubject) -> ActionPolicyDecision | None:
    if subject.kind != "shell":
        return None
    if not subject.command.strip():
        return ActionPolicyDecision.deny(
            subject,
            guard_id="shell_approval_guard",
            reason_code="command_required",
            display="command required",
        )
    if not subject.approval_available:
        return ActionPolicyDecision.deny(
            subject,
            guard_id="shell_approval_guard",
            reason_code="approval_unavailable",
            display="shell command requires approval, but no approval channel is available",
        )
    return ActionPolicyDecision.ask_user(
        subject,
        guard_id="shell_approval_guard",
        reason_code="requires_user_approval",
        display="shell command requires user approval",
    )


def research_url_guard(subject: ActionSubject) -> ActionPolicyDecision | None:
    if subject.kind != "research_url":
        return None
    reason = research_url_denial_reason(subject.url)
    if reason:
        return ActionPolicyDecision.deny(
            subject,
            guard_id="research_url_guard",
            reason_code=_reason_code(reason),
            display=reason,
        )
    return None


def local_context_action_guard(subject: ActionSubject) -> ActionPolicyDecision | None:
    if subject.kind != "local_context_action":
        return None
    if subject.tool_name in {"prompt_patch", "router_override", "provider_override", "permission_override"}:
        return ActionPolicyDecision.deny(
            subject,
            guard_id="local_context_action_guard",
            reason_code="local_context_scope_violation",
            display="local context cannot change prompt, router, provider, or permissions",
        )
    return None


def provider_fallback_guard(subject: ActionSubject) -> ActionPolicyDecision | None:
    if subject.kind != "provider_fallback":
        return None
    if not subject.from_provider or not subject.to_provider:
        return ActionPolicyDecision.deny(
            subject,
            guard_id="provider_fallback_guard",
            reason_code="provider_required",
            display="provider fallback requires source and target providers",
        )
    if subject.from_provider == subject.to_provider:
        return ActionPolicyDecision.deny(
            subject,
            guard_id="provider_fallback_guard",
            reason_code="same_provider",
            display="provider fallback target must differ from source",
        )
    return None


def managed_output_size_guard(subject: ActionSubject) -> ActionPolicyDecision | None:
    if subject.kind != "managed_output":
        return None
    if subject.item_count >= MAX_MANAGED_OUTPUTS_PER_RUN:
        return ActionPolicyDecision.deny(
            subject,
            guard_id="managed_output_size_guard",
            reason_code="managed_output_count_limit",
            display="managed output limit reached for this run",
        )
    if subject.byte_count > MAX_MANAGED_OUTPUT_BYTES:
        return ActionPolicyDecision.deny(
            subject,
            guard_id="managed_output_size_guard",
            reason_code="managed_output_size_limit",
            display="managed output is too large to retain completely",
        )
    return None


def research_url_denial_reason(url: str, *, resolve: bool = True) -> str | None:
    return DEFAULT_NETWORK_POLICY.check_url(url, resolve=resolve)


def _profile(subject: ActionSubject) -> PermissionProfile | None:
    if not subject.permission_profile:
        return None
    try:
        return profile_for_name(subject.permission_profile)
    except ValueError:
        return None


def _permission_denied(subject: ActionSubject) -> ActionPolicyDecision:
    return ActionPolicyDecision.deny(
        subject,
        guard_id="permission_profile_guard",
        reason_code="permission_profile_denied",
        display="action denied by permission profile",
    )


def _reason_code(reason: str) -> str:
    text = reason.lower().replace("(", " ").replace(")", " ")
    code = "_".join(part for part in text.replace("/", " ").split() if part)
    return _identifier(code, MAX_REASON_CHARS) or "research_url_denied"


def _identifier(value: object, limit: int = 80) -> str:
    text = str(value or "").strip().lower()
    text = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in text)
    text = "_".join(part for part in text.split("_") if part)
    if not text or text[0].isdigit():
        return ""
    return text[:limit]


def _clip(value: object, limit: int) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return text if len(text) <= limit else text[:limit].rstrip()


def _clip_display(value: object) -> str:
    return _clip(value, MAX_DISPLAY_CHARS)


def _clip_ref(value: object) -> str:
    text = str(value or "").strip()
    if text.startswith("action:") and len(text) == len("action:") + 64:
        return text
    return _clip(text, 80)


def _nonnegative_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, float) and math.isfinite(value):
        return max(int(value), 0)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return 0


def _normcase_path(value: object) -> str:
    try:
        return str(Path(str(value or "")).expanduser().resolve())
    except (OSError, RuntimeError, ValueError):
        return str(value or "")


def digest_text(value: object) -> str:
    text = str(value or "")
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


__all__ = [
    "ActionPolicyDecision",
    "ActionPolicyPipeline",
    "ActionSubject",
    "DECISION_ALLOW",
    "DECISION_ASK_USER",
    "DECISION_DENY",
    "MAX_MANAGED_OUTPUT_BYTES",
    "MAX_MANAGED_OUTPUTS_PER_RUN",
    "evaluate_action",
]
