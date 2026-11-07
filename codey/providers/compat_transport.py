"""Finite legacy-provider adaptation; dynamic method lookup stays at this boundary."""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

from codey.providers.request_codec import provider_options_payload
from codey.providers.requests import (
    AcknowledgeToolResultsBoundRequest,
    AcknowledgeToolResultsRequest,
    ProviderRequest,
    SendTextRequest,
    SendToolResultsBoundRequest,
    SendToolResultsRequest,
    SendTurnRequest,
)


def require_callable(provider: object, name: str) -> Callable[..., object]:
    method = getattr(provider, name, None)
    if not callable(method):
        raise TypeError(f"provider does not support {name}")
    # callable() establishes this deliberately open third-party call boundary.
    # Request fields and method selection are validated before invocation.
    return cast(Callable[..., object], method)


def execute_compat_request(provider: object, request: ProviderRequest) -> object:
    """Translate a validated finite request to an existing provider's exact API."""
    options = provider_options_payload(request.options)
    if isinstance(request, SendTextRequest):
        return require_callable(provider, "send")(request.prompt, **options)
    tools = None if request.tools is None else list(request.tools)
    if isinstance(request, SendTurnRequest):
        return require_callable(provider, "send_turn")(request.prompt, tools, **options)
    results = list(request.results)
    if isinstance(request, SendToolResultsRequest):
        return require_callable(provider, "send_tool_results")(results, tools, **options)
    if isinstance(request, AcknowledgeToolResultsRequest):
        return require_callable(provider, "acknowledge_tool_results")(results, tools, **options)
    if isinstance(request, SendToolResultsBoundRequest):
        return require_callable(provider, "send_tool_results_bound")(results, request.window_ref, tools, **options)
    if isinstance(request, AcknowledgeToolResultsBoundRequest):
        return require_callable(provider, "acknowledge_tool_results_bound")(results, request.window_ref, tools, **options)
    raise TypeError("unknown provider request")
