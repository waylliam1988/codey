"""Chat provider package.

The provider package contains both small data modules and heavyweight web
drivers. Keep package import cheap: public convenience exports are resolved
only when requested, so importing ``codey.providers.diagnostics`` does
not also import every browser driver.
"""

from __future__ import annotations

from typing import Any

_EXPORTS: dict[str, tuple[str, str]] = {
    "ChatProvider": ("codey.providers.base", "ChatProvider"),
    "LocalOpenAIProvider": ("codey.providers.local_openai", "LocalOpenAIProvider"),
    "DEFAULT_PROVIDER_ID": ("codey.providers.registry", "DEFAULT_PROVIDER_ID"),
    "PROVIDER_LABELS": ("codey.providers.registry", "PROVIDER_LABELS"),
    "borrow_open_provider": ("codey.providers.registry", "borrow_open_provider"),
    "connect_existing_provider": ("codey.providers.registry", "connect_existing_provider"),
    "connect_fresh_provider_tab": ("codey.providers.registry", "connect_fresh_provider_tab"),
    "connect_provider": ("codey.providers.registry", "connect_provider"),
    "provider_tab_availability": ("codey.providers.registry", "provider_tab_availability"),
    "provider_ids": ("codey.providers.registry", "provider_ids"),
    "warm_provider_tabs": ("codey.providers.registry", "warm_provider_tabs"),
    "DeepSeekWebProvider": ("codey.providers.web_provider", "DeepSeekWebProvider"),
    "GlmWebProvider": ("codey.providers.web_provider", "GlmWebProvider"),
    "MimoWebProvider": ("codey.providers.web_provider", "MimoWebProvider"),
    "QwenWebProvider": ("codey.providers.web_provider", "QwenWebProvider"),
    "StepFunWebProvider": ("codey.providers.web_provider", "StepFunWebProvider"),
    "WebChatProvider": ("codey.providers.web_provider", "WebChatProvider"),
    "WebProviderSpec": ("codey.providers.web_provider", "WebProviderSpec"),
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    try:
        module_name, attr_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    module = __import__(module_name, fromlist=[attr_name])
    value = getattr(module, attr_name)
    globals()[name] = value
    return value
