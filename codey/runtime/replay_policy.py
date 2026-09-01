"""Tool and runtime effect replay policy.

Classifies external effects into safe (pure information, idempotent projection)
and unsafe (filesystem edits, command execution, shell approval, provider calls,
repair rounds, and unknown tools).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

SAFE_TOOL_NAMES = frozenset({
    "read",
    "ls",
    "search",
    "references",
    "project_facts",
    "project_map",
})

REPLAYABLE_SAFE_TOOL_NAMES = frozenset({
    "read",
    "ls",
    "search",
    "references",
})


def is_replayable_safe_tool(tool_name: str) -> bool:
    """Return True if tool_name is in the replayable safe tools whitelist."""
    return str(tool_name or "").strip() in REPLAYABLE_SAFE_TOOL_NAMES


UNSAFE_TOOL_NAMES = frozenset({
    "edit",
    "write",
    "shell",
    "run",
    "knowledge_write",
})


class ReplayClass:
    SAFE = "safe"
    UNSAFE = "unsafe"


ReplayClassType = Literal["safe", "unsafe"]


@dataclass(frozen=True)
class ReplayDecision:
    replay_class: ReplayClassType
    reason: str


def tool_replay_policy(
    tool_name: str,
    *,
    policy_denied: bool = False,
    approval_required: bool = False,
) -> ReplayDecision:
    """Classify a tool call into a safe or unsafe replay decision.

    'run' is unconditionally unsafe regardless of command string.
    Unknown tools default to unsafe fail-closed.
    """
    canonical_name = str(tool_name or "").strip()
    if policy_denied:
        return ReplayDecision(
            replay_class=ReplayClass.UNSAFE,
            reason="policy_denied",
        )
    if approval_required or canonical_name == "shell":
        return ReplayDecision(
            replay_class=ReplayClass.UNSAFE,
            reason="approval_required" if approval_required else "shell_command",
        )
    if canonical_name in SAFE_TOOL_NAMES:
        return ReplayDecision(
            replay_class=ReplayClass.SAFE,
            reason="read_only_tool",
        )
    if canonical_name in UNSAFE_TOOL_NAMES:
        return ReplayDecision(
            replay_class=ReplayClass.UNSAFE,
            reason="mutating_or_executing_tool",
        )
    return ReplayDecision(
        replay_class=ReplayClass.UNSAFE,
        reason="unknown_tool",
    )


def provider_replay_policy(purpose: str = "") -> ReplayDecision:
    """Classify an outbound provider prompt send."""
    return ReplayDecision(
        replay_class=ReplayClass.UNSAFE,
        reason="outbound_provider_call",
    )


def repair_replay_policy() -> ReplayDecision:
    """Classify a bounded completion repair round."""
    return ReplayDecision(
        replay_class=ReplayClass.UNSAFE,
        reason="completion_repair_round",
    )


__all__ = [
    "REPLAYABLE_SAFE_TOOL_NAMES",
    "ReplayClass",
    "ReplayClassType",
    "ReplayDecision",
    "SAFE_TOOL_NAMES",
    "UNSAFE_TOOL_NAMES",
    "is_replayable_safe_tool",
    "provider_replay_policy",
    "repair_replay_policy",
    "tool_replay_policy",
]
