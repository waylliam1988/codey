"""Native OpenAI tool-call lowering to canonical ToolPlan.

Only lowers ``AssistantTurn`` (already-parsed provider structs) to the
canonical IR. Argument validation still goes through ``normalize_tool_args``;
native never bypasses Codey checks. Text-only turns fall back to the existing
JSON codec.
"""

from __future__ import annotations

from typing import Any

from codey.protocols.json_codec import JsonToolCodec
from codey.providers.base import AssistantTurn
from codey.runtime.core.models import Control, ToolCall, ToolPlan
from codey.toolchain import definition as tool_defs
from codey.toolchain.runtime import MAX_REPLACEMENTS, READ_MAX_LINES
from codey.toolchain.tool_args_repair import (
    ToolArgLimits,
    ToolArgsRepairError,
    normalize_tool_args,
)

_DEFAULT_LIMITS = ToolArgLimits(
    max_replacements=MAX_REPLACEMENTS,
    read_max_lines=READ_MAX_LINES,
)


class NativeToolResultError(ValueError):
    """Raised when a native tool result cannot be chained to its call."""


class NativeOpenAIToolCodec:
    name = "native_openai"

    def __init__(self, *, permission_profile: str = "coding_writer", json_fallback: JsonToolCodec | None = None) -> None:
        self._fallback = json_fallback or JsonToolCodec(permission_profile=permission_profile)
        self.permission_profile = permission_profile

    def system_prompt(self) -> str:
        return self._fallback.system_prompt()

    def model_tool_contract_hash(self) -> str:
        return self._fallback.model_tool_contract_hash()

    def repair_prompt(self) -> str:
        return self._fallback.repair_prompt()

    def public_example(self, tool_name: str) -> str:
        return self._fallback.public_example(tool_name)

    def format_results(self, results: list) -> str:
        return self._fallback.format_results(results)

    def parse_turn(self, turn: AssistantTurn) -> ToolPlan:
        calls: list[ToolCall] = []
        raw_calls = list(turn.tool_calls or ())
        if len(raw_calls) > tool_defs.MAX_ACCIDENTAL_TOOL_CALLS:
            # Never silently drop the tail: every call id needs an answer to
            # keep the provider chain legal, so fail the whole turn and let
            # the synthetic-error path answer all ids at once.
            return ToolPlan(
                calls=[],
                control=None,
                protocol_error=(
                    f"too many native tool calls in one turn ({len(raw_calls)}); "
                    f"send at most {tool_defs.MAX_ACCIDENTAL_TOOL_CALLS}"
                ),
                protocol_error_kind="too_many_tools",
            )
        structural = self._precheck_turn_shape(raw_calls)
        if structural is not None:
            return structural
        if raw_calls:
            for item in raw_calls:
                plan = self._parse_native_call(item.name, item.arguments, item.id)
                if plan.protocol_error:
                    return plan
                calls.extend(plan.calls)
                if plan.control is not None and plan.control.kind == "done" and not calls:
                    return plan
            if calls:
                body = "Need tool result" if len(calls) == 1 else "Need tool results"
                return ToolPlan(calls=calls, control=Control(kind="continue", body=body))
        # No structured calls: fall back to JSON text parsing (model spoke prose).
        return self._fallback.parse(turn.text or "")

    @staticmethod
    def _precheck_turn_shape(raw_calls: list) -> ToolPlan | None:
        """Reject unanswerable or ambiguous turns before any tool executes.

        A missing call id can never be answered legally, and a `done` mixed
        with other calls has no single meaning (first- or last-wins would
        silently drop work). Fail the whole turn so the synthetic-error path
        answers every original call id at once.
        """
        if not raw_calls:
            return None
        missing = [
            str(getattr(item, "name", "") or "?")
            for item in raw_calls
            if not str(getattr(item, "id", "") or "")
        ]
        if missing:
            return ToolPlan(
                calls=[],
                control=None,
                protocol_error=(
                    "native tool call without an id cannot be answered: "
                    + ", ".join(missing)
                ),
                protocol_error_kind="invalid_args",
            )
        names = [str(getattr(item, "name", "") or "").strip().lower() for item in raw_calls]
        if "done" in names and len(raw_calls) != 1:
            return ToolPlan(
                calls=[],
                control=None,
                protocol_error="done must be the only call in a turn",
                protocol_error_kind="too_many_tools",
            )
        return None

    def _parse_native_call(self, name: str, args: Any, call_id: str) -> ToolPlan:
        normalized = str(name or "").strip().lower()
        if not normalized:
            return ToolPlan(calls=[], control=None, protocol_error="empty native tool name",
                            protocol_error_kind="unknown_tool")
        if not str(call_id or ""):
            # Belt-and-braces behind _precheck_turn_shape: no id-less call
            # may ever reach execution, even on a direct single-call path.
            return ToolPlan(calls=[], control=None,
                            protocol_error=f"native tool call without an id cannot be answered: {normalized}",
                            protocol_error_kind="invalid_args", protocol_tool_name=normalized)
        # Map OpenAI function name (runtime name) back to canonical model name.
        runtime_def = tool_defs.RUNTIME_TOOL_DEFINITION_BY_NAME.get(normalized)
        model_name = runtime_def.name if runtime_def is not None else normalized
        if model_name == "done":
            summary = ""
            if isinstance(args, dict):
                summary = str(args.get("summary") or "")
            return ToolPlan(calls=[], control=Control(kind="done", body=summary or "done"))
        if not isinstance(args, dict):
            return ToolPlan(calls=[], control=None, protocol_error=f"{normalized} args must be an object",
                            protocol_error_kind="invalid_args", protocol_tool_name=normalized)
        spec = tool_defs.TOOL_DEFINITION_BY_NAME.get(model_name)
        if spec is None:
            return ToolPlan(calls=[], control=None, protocol_error=f"unknown tool: {name}",
                            protocol_error_kind="unknown_tool", protocol_tool_name=normalized)
        if spec.runtime_name is None:
            return ToolPlan(calls=[], control=None,
                            protocol_error=f"tool not available natively: {name}",
                            protocol_error_kind="unknown_tool", protocol_tool_name=normalized)
        try:
            repair = normalize_tool_args(spec.runtime_name, args, limits=_DEFAULT_LIMITS)
        except ToolArgsRepairError as exc:
            return ToolPlan(calls=[], control=None, protocol_error=str(exc),
                            protocol_error_kind=exc.repair_kind, protocol_tool_name=spec.runtime_name)
        call = ToolCall(name=spec.runtime_name, args=repair.args, call_id=str(call_id or ""))
        return ToolPlan(calls=[call], control=Control(kind="continue", body="Need tool result"))

    @staticmethod
    def tool_messages(results: list) -> list[dict[str, object]]:
        """Build ``role: tool`` messages; fail closed on a missing call id.

        Silently dropping a result would leave the provider with an
        assistant ``tool_calls`` block that has no matching tool message,
        and most OpenAI-compatible servers reject that chain with a 400.
        """
        messages: list[dict[str, object]] = []
        missing: list[str] = []
        for index, result in enumerate(results):
            call_id = str(getattr(result.call, "call_id", "") or "")
            content = str(getattr(result, "model_text", "") or "")
            if not call_id:
                missing.append(f"{index}:{getattr(result.call, 'name', '?')}")
                continue
            messages.append({"role": "tool", "tool_call_id": call_id, "content": content})
        if missing:
            raise NativeToolResultError(
                "native tool result missing call_id for " + ", ".join(missing)
            )
        return messages


def build_native_codec_for_profile(
    profile_name: str,
    fallback_codec: JsonToolCodec | None = None,
) -> tuple[NativeOpenAIToolCodec, list[dict[str, object]]]:
    """Build the native codec plus its tool list for one permission profile.

    Lives in ``protocols`` (not ``agents/loop``) so the agent loop keeps its
    architecture boundary: loop imports protocols, protocols owns toolchain.
    """

    from codey.toolchain.openai_tools import render_openai_tools
    from codey.toolchain.registry import ToolRegistry

    snapshot = ToolRegistry().snapshot(profile_name=profile_name, mode="coding")
    return NativeOpenAIToolCodec(permission_profile=profile_name, json_fallback=fallback_codec), render_openai_tools(
        snapshot.definitions
    )


__all__ = ["NativeOpenAIToolCodec", "NativeToolResultError", "build_native_codec_for_profile"]
