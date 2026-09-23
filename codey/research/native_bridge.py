"""Structured-provider bridge for the research loop.

Keeps ``research/runner.py`` under its size ceiling: capability gating,
tool-list rendering, and provider send wrappers live here. Runner keeps thin
method shells.
"""

from __future__ import annotations

import os
from collections import deque
from dataclasses import replace
from typing import Any

from codey.env_names import NATIVE_TOOLS_ENV

_RESEARCH_ATTEMPT_WINDOW = 24


def use_native_provider(provider: Any, provider_id: str = "") -> bool:
    if not (callable(getattr(provider, "send_turn", None)) and callable(getattr(provider, "send_tool_results", None))):
        return False
    raw = os.environ.get(NATIVE_TOOLS_ENV, "").strip().lower()
    if raw in {"1", "true", "yes", "y", "on"}:
        return True
    if raw in {"0", "false", "no", "n", "off"}:
        return False
    # Unit-test doubles (unittest.mock.Mock) auto-create every attribute,
    # so a plain Mock falsely advertises structured support. Only an
    # explicit NATIVE_TOOLS=1 (handled above) may force native for mocks.
    try:
        from unittest.mock import Mock as _Mock

        if isinstance(provider, _Mock):
            return False
    except Exception:
        pass
    try:
        from codey.providers.ids import normalize_provider_id

        pid = normalize_provider_id(provider_id or getattr(provider, "name", "") or "")
    except Exception:
        pid = str(provider_id or getattr(provider, "name", "") or "").strip().lower()
    if pid == "local":
        try:
            from codey.providers.local_config import load_local_config, resolve_local_native_tools

            return bool(resolve_local_native_tools(load_local_config()))
        except Exception:
            pass
    try:
        from codey.providers.capabilities import capability_for

        capability = capability_for(str(provider_id or getattr(provider, "name", "") or ""))
        return bool(getattr(capability, "native_tools_default", False))
    except Exception:
        return False


def native_research_tools(include_source_search: bool = True) -> list[dict[str, object]] | None:
    try:
        from codey.research.tool_contract import render_openai_tools

        return render_openai_tools(include_source_search=include_source_search)
    except Exception:
        return None


def turn_text(turn: object) -> str:
    if isinstance(turn, str):
        return turn
    text = str(getattr(turn, "text", "") or "")
    calls = getattr(turn, "tool_calls", ()) or ()
    if calls:
        names = ", ".join(str(getattr(call, "name", "")) for call in calls)
        summary = f"[tool_calls: {names}]"
        return f"{text}\n{summary}" if text else summary
    return text


def send_provider_structured(runner: object, message: str) -> object:
    from codey.runtime.core import cancellation
    from codey.runtime.observe.prompt_envelope import record_provider_send_prompt

    cancellation.check()
    runner._bind_pending_intro_rows(message)
    runner._research_send_seq += 1
    record_provider_send_prompt(
        runner.trace_recorder,
        name="research_outbound_prompt",
        text=message,
        purpose="research prompt sent to provider",
        source_ref="provider_send:research",
        capability_id="research_runner",
        phase="research",
        send_ref=f"research_send:{runner._research_send_seq}",
        provider_effect_id="",
        model_tool_contract_hash=runner.codec.model_tool_contract_hash(),
        runtime_tool_contract_hash="",
    )
    try:
        tools = native_research_tools(bool(getattr(runner.codec, "include_source_search", True)))
        return runner.provider.send_turn(message, tools, timeout=None)
    except Exception as exc:
        runner._record_model_failure("send", exc)
        raise


def send_native_tool_results(runner: object, tool_messages: list[dict[str, object]]) -> object:
    try:
        tools = native_research_tools(bool(getattr(runner.codec, "include_source_search", True)))
        return runner.provider.send_tool_results(tool_messages, tools, timeout=None)
    except Exception as exc:
        runner._record_model_failure("send", exc)
        raise


def take_pending_or_send(runner: object, outbound: str) -> object:
    pending = getattr(runner, "_pending_native_turn", None)
    if pending is not None:
        runner._pending_native_turn = None
        return pending
    return runner._send_provider_structured(outbound)


def maybe_prefetch_native_turn(
    runner: object,
    plan: object,
    tool_results: list,
    guard_message: str = "",
) -> bool:
    if not runner._use_native_provider() or not getattr(plan, "calls", ()):
        return False
    tool_messages = runner.codec.tool_messages(tool_results)
    if not tool_messages:
        return False
    if guard_message:
        last = dict(tool_messages[-1])
        last["content"] = str(last.get("content") or "") + guard_message
        tool_messages[-1] = last
    runner._pending_native_turn = runner._send_native_tool_results(tool_messages)
    return True


def answer_native_protocol_error(runner: object, reply_turn: object, plan: object) -> bool:
    """Answer dangling native tool_calls with synthetic ERROR tool messages.

    A text repair prompt after an assistant ``tool_calls`` block would break
    the OpenAI tool-call chain; this keeps it legal and lets the model retry.
    """
    ids = [str(getattr(call, "id", "") or "") for call in (getattr(reply_turn, "tool_calls", ()) or ())]
    ids = [call_id for call_id in ids if call_id]
    if not ids:
        return False
    err = str(getattr(plan, "protocol_error", "") or "invalid native tool call")
    runner._pending_native_turn = runner._send_native_tool_results(
        [{"role": "tool", "tool_call_id": call_id, "content": f"ERROR: {err}"} for call_id in ids]
    )
    return True


def next_message(runner: object, plan: object, pairs: list, tool_results: list) -> str | None:
    """Guard first, then prefetch; None means the next turn is already pending."""
    guard = record_and_check_cycle(runner, plan, pairs)
    if maybe_prefetch_native_turn(runner, plan, tool_results, guard):
        return None
    return runner._format_results(tool_results) + guard


def record_and_check_cycle(runner: object, plan: object, pairs: list) -> str:
    """Record research attempts and return a guard reminder when cyclically stuck."""
    attempts = getattr(runner, "_research_attempts", None)
    if attempts is None:
        attempts = deque(maxlen=_RESEARCH_ATTEMPT_WINDOW)
        runner._research_attempts = attempts
    if getattr(plan, "calls", ()) and pairs:
        from codey.agents.runaway_guard import attempt_record

        for call, outcome in pairs:
            try:
                record = attempt_record(call, outcome, turn=0)
            except Exception:
                continue
            if bool(getattr(outcome, "changed", False)) and not record.changed:
                record = replace(record, changed=True)
            attempts.append(record)
    if len(attempts) < 4:
        return ""
    try:
        from codey.agents.runaway_guard import detect_periodic_cycle

        decision = detect_periodic_cycle(attempts)
    except Exception:
        return ""
    if decision.block:
        return "\n\n[loop guard] " + decision.reason
    return ""


__all__ = [
    "answer_native_protocol_error",
    "maybe_prefetch_native_turn",
    "native_research_tools",
    "next_message",
    "record_and_check_cycle",
    "send_native_tool_results",
    "send_provider_structured",
    "take_pending_or_send",
    "turn_text",
    "use_native_provider",
]
