"""Typed runtime capability view used by the shared task kernel.

The durable API selection remains the source of truth for the admitted model
and protocol. This small view only gathers the provider-instance facts that
the kernel needs while executing one request, so the kernel does not repeat
ad-hoc ``getattr`` probes at each decision point.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, cast

from codey.operations import kernel_transport

TextToolFormat = Literal["plain", "json_schema"]


@dataclass(frozen=True)
class KernelCapabilityView:
    """Provider facts observed for one kernel execution."""

    native_tools: bool
    text_tool_format: TextToolFormat = "plain"
    request_scoped_tool_context: bool = False
    window_ref: Any = None

    @classmethod
    def from_provider(
        cls,
        provider: Any,
        *,
        provider_id: object = "",
        fallback_window_ref: Any = None,
    ) -> KernelCapabilityView:
        raw_format = getattr(provider, "text_tool_format", "plain")
        text_tool_format = cast(TextToolFormat, raw_format) if raw_format in {"plain", "json_schema"} else "plain"
        return cls(
            native_tools=kernel_transport.provider_uses_native(provider, provider_id=provider_id),
            text_tool_format=text_tool_format,
            request_scoped_tool_context=getattr(provider, "request_scoped_tool_context", False) is True,
            window_ref=getattr(provider, "window_ref", None) or fallback_window_ref,
        )


__all__ = ["KernelCapabilityView", "TextToolFormat"]
