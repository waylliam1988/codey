"""Provider transport primitives for the shared task kernel.

This module owns provider method selection and native call-id repair. The task
loop decides *when* to send; this module decides *how* to invoke the provider.
"""

from __future__ import annotations

from typing import Any


def call_provider_send(provider: Any, prompt: str) -> Any:
    return provider.send(prompt)


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


def repair_native_dangling(
    provider: Any,
    reply: Any,
    native: bool,
    native_tools: Any,
    error: str,
) -> Any:
    """Close native call ids after a protocol error so the provider chain stays valid."""
    if not native or isinstance(reply, str):
        return None
    try:
        ids = [str(getattr(call, "id", "") or "") for call in (getattr(reply, "tool_calls", ()) or ())]
    except Exception:
        return None
    ids = [item for item in ids if item]
    if not ids:
        return None
    try:
        return call_provider_send_results(
            provider,
            [{"role": "tool", "tool_call_id": item, "content": f"ERROR: {error}"} for item in ids],
            native_tools,
        )
    except Exception:
        return None


__all__ = [
    "call_provider_send",
    "call_provider_send_results",
    "call_provider_send_turn",
    "repair_native_dangling",
    "send_kernel_reply",
]
