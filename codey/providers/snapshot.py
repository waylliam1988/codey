"""Provider request interpretation frozen at durable task admission."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from codey.runtime.core.api_selection import ApiRunSelection


def provider_snapshot(
    provider_id: str,
    selection: ApiRunSelection | None,
    *,
    provider_session_id: str = "",
) -> dict[str, object]:
    """Freeze the provider request contract when the task is admitted."""
    if selection is None:
        return {
            "provider_id": provider_id,
            "model_id": None,
            "protocol": "provider-native",
            "generation_parameters": {},
            "capabilities": {
                "supports_native_session": True,
                "accepts_history_replay": False,
                "supports_tool_calls": False,
                "supports_images": None,
                "supports_reasoning": None,
                "max_context_tokens": None,
            },
            "tokenizer_id": None,
            "provider_session_id": provider_session_id,
            "model_selection": {},
        }
    return {
        "provider_id": selection.connection_id,
        "model_id": selection.model_id,
        "protocol": selection.protocol,
        "generation_parameters": {
            "thinking_enabled": selection.thinking_enabled,
            "reasoning_effort": selection.reasoning_effort,
            "stream": selection.stream,
            "tool_choice": selection.tool_choice,
            "output_tokens": selection.output_tokens,
        },
        "capabilities": {
            "supports_native_session": False,
            "accepts_history_replay": True,
            "supports_tool_calls": selection.native_tools,
            "supports_images": None,
            "supports_reasoning": selection.thinking_enabled is not None or selection.reasoning_effort is not None,
            "max_context_tokens": selection.context_window_tokens,
        },
        "tokenizer_id": selection.token_counter,
        "provider_session_id": provider_session_id,
        "context_reserve_tokens": selection.context_reserve_tokens,
        "context_keep_recent_tokens": selection.context_keep_recent_tokens,
        "budget_source": selection.budget_source,
        "input_limit_tokens": selection.input_limit_tokens,
        "model_selection": selection.to_payload(),
    }


def capture_provider_selection(
    provider_id: str,
    selection: ApiRunSelection | Mapping[str, object] | None,
) -> ApiRunSelection | None:
    """Capture a validated API selection before the task is admitted."""
    from codey.providers.catalog import API_CONNECTIONS

    if provider_id not in API_CONNECTIONS:
        return None
    from codey.runtime.core.api_selection import ApiRunSelection

    if isinstance(selection, ApiRunSelection):
        if selection.connection_id != provider_id:
            raise ValueError("selected API connection does not match provider")
        return selection
    from codey.providers.api_connections import capture_selection

    return capture_selection(provider_id, selection)


__all__ = ["capture_provider_selection", "provider_snapshot"]
