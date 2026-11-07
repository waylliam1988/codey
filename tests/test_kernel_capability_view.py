from __future__ import annotations

from types import SimpleNamespace

import pytest

from codey.operations.kernel_capabilities import KernelCapabilityView
from codey.runtime.core.protocol_window import ProtocolWindowRef, ProviderBinding


def _window(identity: str) -> ProtocolWindowRef:
    return ProtocolWindowRef("session", ProviderBinding("id", "rev", "model", "text", "contract"), identity)


def test_view_freezes_provider_instance_facts(monkeypatch) -> None:
    monkeypatch.setattr("codey.operations.kernel_transport.provider_uses_native", lambda *_args, **_kwargs: True)
    provider = SimpleNamespace(
        text_tool_format="json_schema",
        request_scoped_tool_context=True,
        window_ref=_window("provider-window"),
    )

    view = KernelCapabilityView.from_provider(provider, provider_id="local", fallback_window_ref=_window("transport-window"))

    assert view.native_tools is True
    assert view.text_tool_format == "json_schema"
    assert view.request_scoped_tool_context is True
    assert view.window_ref == _window("provider-window")


def test_view_uses_transport_window_when_provider_has_no_window() -> None:
    view = KernelCapabilityView.from_provider(SimpleNamespace(native_tools=False), fallback_window_ref=_window("transport-window"))

    assert view.window_ref == _window("transport-window")
    assert view.text_tool_format == "plain"
    assert view.request_scoped_tool_context is False


def test_view_rejects_unknown_text_format_to_plain() -> None:
    view = KernelCapabilityView.from_provider(SimpleNamespace(text_tool_format="future-format"))

    assert view.text_tool_format == "plain"


def test_view_rejects_unstructured_window_reference() -> None:
    with pytest.raises(ValueError, match="window reference"):
        KernelCapabilityView.from_provider(SimpleNamespace(window_ref="arbitrary"))
