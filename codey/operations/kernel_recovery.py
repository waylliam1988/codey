"""Recovery prompt shaping for the shared task kernel."""

from __future__ import annotations

from typing import Any


class RecoveryFailed(RuntimeError):
    """Recovered tool results could not be delivered; the run must stop."""


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
    """Deliver recovered results before accepting new model tool calls.

    Any formatting or native-message construction failure raises
    :class:`RecoveryFailed`: the caller must terminate as a recovery failure
    without sending the initial prompt. Silently dropping recovered results
    and continuing the model dialogue is forbidden.
    """
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
    except RecoveryFailed:
        raise
    except Exception as exc:
        raise RecoveryFailed(f"recovery delivery failed: {exc}") from exc


__all__ = ["RecoveryFailed", "apply_recovery_first"]
