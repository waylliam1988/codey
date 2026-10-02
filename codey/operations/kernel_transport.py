"""Provider transport for the shared task kernel.

Provider capability selection, sending, native call-id receipts, and budget
drain. Failures raise explicit errors; the task loop maps them to results.
"""

from __future__ import annotations

from typing import Any

from codey.operations.provider_session import normalize_provider_reply


class NativeBudgetExhausted(RuntimeError):
    """Native chain kept emitting tool calls past the budget drain limit."""


def call_provider_send(provider: Any, prompt: str) -> Any:
    reply = provider.send(prompt)
    return normalize_provider_reply(provider, reply)


def call_provider_send_turn(provider: Any, prompt: str, tools: Any) -> Any:
    return provider.send_turn(prompt, tools)


def call_provider_send_results(provider: Any, messages: Any, tools: Any) -> Any:
    return provider.send_tool_results(messages, tools)


def send_kernel_reply(
    provider: Any,
    native: bool,
    prompt: str,
    native_tools: Any,
    pending_reply: Any,
    pending_native_messages: list[dict[str, Any]] | None,
) -> tuple[Any, Any, list[dict[str, Any]] | None]:
    """Send exactly one provider message and clear the consumed pending slot."""
    if pending_reply is not None:
        return pending_reply, None, pending_native_messages
    if native and pending_native_messages is not None:
        reply = call_provider_send_results(provider, pending_native_messages, native_tools)
        return reply, None, None
    if native:
        return call_provider_send_turn(provider, prompt, native_tools), None, pending_native_messages
    return call_provider_send(provider, prompt), None, pending_native_messages


def provider_uses_native(provider: Any, *, provider_id: object = "") -> bool:
    """Reuse the production native-tool decision; never bare hasattr checks.

    The protocol is decided once at task start. An identification failure
    raises instead of silently falling back to web: continuing on the wrong
    protocol would mis-deliver native call ids.
    """
    from codey.operations.provider_session import ProviderAdapter
    from codey.providers.native_tools import supports_native_tools

    probe = provider
    seen: set[int] = set()
    while isinstance(probe, ProviderAdapter):
        if id(probe) in seen:
            raise ValueError("cyclic provider adapter chain")
        seen.add(id(probe))
        probe = probe.provider
    return bool(supports_native_tools(probe, str(provider_id or "")))


def _native_tool_messages(results: Any, session: Any) -> list[dict[str, Any]]:
    from codey.operations.kernel_prompt import _result_context

    rows = list(results or [])
    ids = [str(getattr(getattr(r, "call", None), "call_id", "") or "") for r in rows]
    has_id = [bool(i) for i in ids]
    if rows and any(has_id) and not all(has_id):
        # Mixed batches would silently drop the valid call ids if we fell
        # back to text; fail closed instead. Cross-provider text delivery
        # only happens in the explicit provider_session_changed branch.
        raise ValueError(
            "mixed native batch: some results lack call ids; "
            "refusing to deliver a partial native chain"
        )
    messages: list[dict[str, Any]] = []
    for result in rows:
        call_id = str(getattr(result.call, "call_id", "") or "")
        if not call_id:
            # JSON-originated calls in a native session have no chain id;
            # synthesize a follow-up prompt instead of a tool message.
            return []
        messages.append({"role": "tool", "tool_call_id": call_id, "content": _result_context(result, session)})
    return messages


def _take_answered_reply(
    provider: Any,
    reply: Any,
    native: bool,
    native_tools: Any,
    followup: str,
) -> Any:
    """Answer one native rejection so every call id is closed.

    Returns the provider's answer, or None when there is nothing to answer
    (web reply, or a native reply without call ids, which normalize_turn
    already rejects upstream). A receipt send failure raises: callers must
    report provider_failure and stop instead of continuing with unanswered
    ids. Valid ids always get their error result; only a missing-id call,
    which can never be legally receipted, terminates without one.
    """
    if not native or isinstance(reply, str):
        return None
    try:
        ids = [str(getattr(c, "id", "") or "") for c in (getattr(reply, "tool_calls", ()) or [])]
    except Exception:
        return None
    ids = list(dict.fromkeys(i for i in ids if i))
    if not ids:
        return None
    content = str(followup or "")
    if not content.startswith("OK:"):
        content = f"ERROR: {content}"
    return call_provider_send_results(
        provider,
        [{"role": "tool", "tool_call_id": i, "content": content} for i in ids],
        native_tools,
    )


def _drain_native_budget(
    provider: Any,
    messages: list[dict[str, Any]],
    *,
    error: str = "turn budget exhausted; tool call was not executed",
) -> None:
    """Deliver the final native batch when the turn budget is exhausted.

    Returns None once the chain is closed (the caller then reports the
    regular budget outcome). A receipt send failure raises, and a chain
    that keeps emitting tool calls past the drain limit raises
    NativeBudgetExhausted; the caller maps both to terminal results.
    """
    for _ in range(4):
        # The task has ended: close ids without inviting another tool call.
        # Still bound and receipt calls from an endpoint ignoring withdrawal.
        reply = call_provider_send_results(provider, messages, [])
        ids = [str(getattr(call, "id", "") or "") for call in (getattr(reply, "tool_calls", ()) or ())]
        ids = list(dict.fromkeys(item for item in ids if item))
        if not ids:
            return None
        messages = [
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": f"ERROR: {error}",
            }
            for call_id in ids
        ]
    raise NativeBudgetExhausted("native tool chain exceeded budget drain limit")


def repair_native_dangling(
    provider: Any,
    reply: Any,
    native: bool,
    native_tools: Any,
    error: str,
) -> Any:
    """Close native call ids after a protocol error so the provider chain stays valid.

    Returns the provider's answer for the error receipts, or None when there
    is nothing to receipt (web reply, unparsable reply, or no call ids).
    A receipt send failure raises: the caller must report provider_failure
    and stop instead of continuing the dialogue with unanswered call ids.
    """
    if not native or isinstance(reply, str):
        return None
    try:
        ids = [str(getattr(call, "id", "") or "") for call in (getattr(reply, "tool_calls", ()) or ())]
    except Exception:
        return None
    ids = list(dict.fromkeys(item for item in ids if item))
    if not ids:
        return None
    return call_provider_send_results(
        provider,
        [{"role": "tool", "tool_call_id": item, "content": f"ERROR: {error}"} for item in ids],
        native_tools,
    )


def close_native_reply(provider: Any, reply: Any, error: str) -> None:
    """Close a terminal reply and bounded follow-on calls without executing them."""
    if isinstance(reply, str):
        return
    ids = list(dict.fromkeys(str(getattr(call, "id", "") or "")
                            for call in (getattr(reply, "tool_calls", ()) or ())))
    messages = [{"role": "tool", "tool_call_id": key, "content": f"ERROR: {error}"}
                for key in ids if key]
    if messages:
        _drain_native_budget(provider, messages, error=error)


__all__ = [
    "NativeBudgetExhausted",
    "call_provider_send",
    "call_provider_send_results",
    "call_provider_send_turn",
    "provider_uses_native",
    "repair_native_dangling",
    "send_kernel_reply",
]
