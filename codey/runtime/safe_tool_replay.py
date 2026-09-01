"""Safe tool replay candidate extraction and canonical arguments validation.

Provides validation and candidate projection for pending safe information tool
intents during crash recovery. Strictly adheres to canonical-only args without
alias rewrites or repair fallbacks.

Does NOT import agents, operations, ghost, provider, or toolchain runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from codey.runtime.effect_records import (
    EFFECT_CATEGORY_TOOL_CALL,
    RuntimeEffectError,
    RuntimeEffectProjection,
)
from codey.runtime.models import ToolCall
from codey.runtime.replay_args import validate_replay_args_shape
from codey.runtime.replay_policy import (
    ReplayClass,
    is_replayable_safe_tool,
)
from codey.tool_args_repair import normalize_tool_args


@dataclass(frozen=True)
class SafeToolReplayCandidate:
    effect_id: str
    call: ToolCall
    turn: int
    tool_index: int


def validate_replay_args(
    tool_name: str,
    args: Mapping[str, object],
) -> dict[str, object]:
    """Strictly validate and return canonical replay args.

    Requires zero alias rewrites and zero arg repairs. Fails closed if args are
    malformed or non-canonical.
    """
    canonical_name = str(tool_name or "").strip()
    if not is_replayable_safe_tool(canonical_name):
        raise RuntimeEffectError(f"tool is not a replayable safe tool: {canonical_name}")
    if not isinstance(args, Mapping) or isinstance(args, bool):
        raise RuntimeEffectError("replay args must be a mapping")

    try:
        shaped_args = validate_replay_args_shape(canonical_name, args)
    except ValueError as exc:
        raise RuntimeEffectError(f"invalid replay args: {exc}") from exc

    try:
        repair_result = normalize_tool_args(canonical_name, shaped_args)
    except Exception as exc:
        raise RuntimeEffectError(f"failed to normalize replay args: {exc}") from exc

    if repair_result.alias_rewrite_count > 0 or repair_result.arg_repair_counts:
        raise RuntimeEffectError(
            "replay args must be strictly canonical without alias rewrites or repairs"
        )

    return dict(repair_result.args)


def replay_args_for_tool_call(call: ToolCall) -> dict[str, object] | None:
    """Extract canonical replay args for a tool call if it is replayable safe.

    Returns None for unsafe or invalid tool calls.
    """
    if not is_replayable_safe_tool(call.name):
        return None
    try:
        return validate_replay_args(call.name, call.args)
    except Exception:
        return None


def candidate_from_effect(
    projection: RuntimeEffectProjection,
) -> SafeToolReplayCandidate | None:
    """Derive a safe tool replay candidate from a pending effect projection.

    Returns None if the effect is not pending, not a safe tool call, has no
    valid canonical replay args, or is otherwise non-replayable.
    """
    if not projection.is_pending:
        return None

    intent = projection.intent
    if intent.effect_category != EFFECT_CATEGORY_TOOL_CALL:
        return None
    if intent.replay_class != ReplayClass.SAFE:
        return None
    if not is_replayable_safe_tool(intent.tool_name):
        return None
    if intent.replay_args is None or not isinstance(intent.replay_args, Mapping):
        return None

    try:
        validated_args = validate_replay_args(intent.tool_name, intent.replay_args)
    except Exception:
        return None

    call = ToolCall(name=intent.tool_name, args=validated_args)
    return SafeToolReplayCandidate(
        effect_id=intent.effect_id,
        call=call,
        turn=intent.turn,
        tool_index=intent.tool_index,
    )


__all__ = [
    "SafeToolReplayCandidate",
    "candidate_from_effect",
    "replay_args_for_tool_call",
    "validate_replay_args",
]
