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
from codey.providers.transport import ProviderTransport
from codey.runtime.core.protocol_window import ProtocolWindowRef


def unwrap_provider(provider: object) -> object:
    seen: set[int] = set()
    while isinstance(provider, ProviderTransport):
        if id(provider) in seen:
            raise ValueError("cyclic provider adapter chain")
        seen.add(id(provider))
        provider = provider.provider
    return provider


def provider_attribute(provider: object, name: str) -> object:
    """Compatibility capability lookup, never an open core interface."""
    unwrap_provider(provider)
    while isinstance(provider, ProviderTransport):
        # Explicit wrapper state (for example an episode timeout) takes
        # precedence; absence never manufactures a capability.
        attributes: dict[str, object] = vars(provider)
        if name in attributes:
            return attributes[name]
        provider = provider.provider
    return getattr(provider, name, None)


def optional_callable(provider: object, name: str) -> Callable[..., object] | None:
    value = provider_attribute(provider, name)
    return value if callable(value) else None


def provider_window_ref(provider: object) -> ProtocolWindowRef | None:
    value = provider_attribute(provider, "window_ref")
    if value is not None and not isinstance(value, ProtocolWindowRef):
        raise ValueError("provider window reference is invalid")
    return value


def declared_callable(provider: object, name: str, *, forwarding_owner: type | None = None) -> Callable[..., object] | None:
    """Find a declared hook; ignore dynamic attributes on providers and mocks."""
    seen: set[int] = set()
    while provider is not None and id(provider) not in seen:
        seen.add(id(provider))
        try:
            attributes: dict[str, object] = vars(provider)
        except TypeError:
            attributes = {}
        if name in attributes:
            value = attributes[name]
            return value if callable(value) else None
        for owner in type(provider).__mro__:
            if name in owner.__dict__:
                if owner is forwarding_owner:
                    break
                value = getattr(provider, name, None)
                return cast(Callable[..., object], value) if callable(value) else None
        provider = attributes.get("provider")
    return None


def require_callable(provider: object, name: str) -> Callable[..., object]:
    method = getattr(provider, name, None)
    if not callable(method):
        raise TypeError(f"provider does not support {name}")
    # callable() establishes this deliberately open third-party call boundary.
    # Request fields and method selection are validated before invocation.
    return cast(Callable[..., object], method)


def execute_compat_request(provider: object, request: ProviderRequest) -> object:
    """Translate a validated finite request to an existing provider's exact API."""
    if isinstance(provider, ProviderTransport):
        # Check cycles before entering wrappers (rather than recursion failure).
        unwrap_provider(provider)
        return provider.execute_request(request)
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
