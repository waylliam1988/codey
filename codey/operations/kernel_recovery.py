"""Recovery prompt shaping for the shared task kernel."""

from __future__ import annotations

from typing import Any


def apply_recovery_first(
    session: Any,
    native: bool,
    pending_initial: list[Any],
    prompt: str,
    pending_native_messages: list[dict[str, Any]] | None,
    *,
    provider_session_changed: bool,
    format_results: Any,
    native_tool_messages: Any,
) -> tuple[str, list[dict[str, Any]] | None]:
    """Deliver recovered results before accepting new model tool calls."""
    if not pending_initial:
        return prompt, pending_native_messages
    try:
        if provider_session_changed:
            return (
                "Continue the unfinished task using the latest local tool results below.\n\n"
                + format_results(pending_initial, session),
                None,
            )
        if native:
            recovered_messages = native_tool_messages(pending_initial, session)
            if recovered_messages:
                return prompt, recovered_messages
        return (
            "Continue the unfinished task using the latest local tool results below.\n\n"
            + format_results(pending_initial, session),
            pending_native_messages,
        )
    except Exception:
        return prompt, pending_native_messages


__all__ = ["apply_recovery_first"]
