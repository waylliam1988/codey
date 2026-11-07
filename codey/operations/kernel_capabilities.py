"""Typed runtime capability view used by the shared task kernel.

The durable API selection remains the source of truth for the admitted model
and protocol. This small view only gathers the provider-instance facts that
the kernel needs while executing one request, so the kernel does not repeat
ad-hoc ``getattr`` probes at each decision point.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from codey.operations import kernel_transport
from codey.providers.compat_transport import provider_attribute, provider_window_ref
from codey.runtime.core.protocol_window import ProtocolWindowRef

TextToolFormat = Literal["plain", "json_schema"]
ProviderWindowRef = ProtocolWindowRef


@runtime_checkable
class ProviderCapabilitySource(Protocol):
    text_tool_format: str
    request_scoped_tool_context: bool
    window_ref: ProtocolWindowRef | None


@dataclass(frozen=True)
class KernelCapabilityView:
    """Provider facts observed for one kernel execution."""

    native_tools: bool
    text_tool_format: TextToolFormat = "plain"
    request_scoped_tool_context: bool = False
    window_ref: ProviderWindowRef | None = None

    @classmethod
    def from_provider(
        cls,
        provider: object,
        *,
        provider_id: object = "",
        fallback_window_ref: ProtocolWindowRef | None = None,
    ) -> KernelCapabilityView:
        raw_format = provider_attribute(provider, "text_tool_format")
        text_tool_format: TextToolFormat = "json_schema" if raw_format == "json_schema" else "plain"
        window_ref = provider_window_ref(provider) or fallback_window_ref
        return cls(
            native_tools=kernel_transport.provider_uses_native(provider, provider_id=provider_id),
            text_tool_format=text_tool_format,
            request_scoped_tool_context=provider_attribute(provider, "request_scoped_tool_context") is True,
            window_ref=window_ref,
        )


__all__ = ["KernelCapabilityView", "ProviderCapabilitySource", "ProviderWindowRef", "TextToolFormat"]
