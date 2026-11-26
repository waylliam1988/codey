from __future__ import annotations

from typing import Protocol

from codey.providers.transport import ProviderTransport


class ProviderHook(Protocol):
    def __call__(self, *args: object, **kwargs: object) -> object: ...


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


def optional_callable(provider: object, name: str) -> ProviderHook | None:
    value = provider_attribute(provider, name)
    return value if callable(value) else None
