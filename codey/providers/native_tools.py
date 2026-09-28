"""Provider capability detection for the shared task kernel."""

from __future__ import annotations

import os
from typing import Any

from codey.env_names import NATIVE_TOOLS_ENV


def supports_native_tools(provider: Any, provider_id: str = "") -> bool:
    """Return whether a provider should use the native tool protocol."""

    if not (
        callable(getattr(provider, "send_turn", None))
        and callable(getattr(provider, "send_tool_results", None))
    ):
        return False
    raw = os.environ.get(NATIVE_TOOLS_ENV, "").strip().lower()
    if raw in {"1", "true", "yes", "y", "on"}:
        return True
    if raw in {"0", "false", "no", "n", "off"}:
        return False
    # unittest.mock.Mock creates arbitrary attributes on demand and therefore
    # must not advertise native support unless the environment explicitly opts in.
    try:
        from unittest.mock import Mock

        if isinstance(provider, Mock):
            return False
    except Exception:
        pass
    try:
        from codey.providers.ids import normalize_provider_id

        pid = normalize_provider_id(provider_id or getattr(provider, "name", "") or "")
    except Exception:
        pid = str(provider_id or getattr(provider, "name", "") or "").strip().lower()
    if pid == "local":
        try:
            from codey.providers.local_config import load_local_config, resolve_local_native_tools

            return bool(resolve_local_native_tools(load_local_config()))
        except Exception:
            return False
    try:
        from codey.providers.capabilities import capability_for

        capability = capability_for(str(provider_id or getattr(provider, "name", "") or ""))
        return bool(getattr(capability, "native_tools_default", False))
    except Exception:
        return False


__all__ = ["supports_native_tools"]
