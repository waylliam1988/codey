"""Chat provider adapters."""

from codey.providers.base import ChatProvider
from codey.providers.local_openai import LocalOpenAIProvider
from codey.providers.registry import (
    DEFAULT_PROVIDER_ID,
    PROVIDER_LABELS,
    borrow_open_provider,
    connect_existing_provider,
    connect_fresh_provider_tab,
    connect_provider,
    provider_tab_availability,
    provider_ids,
    warm_provider_tabs,
)
from codey.providers.web_provider import (
    DeepSeekWebProvider,
    GlmWebProvider,
    MimoWebProvider,
    QwenWebProvider,
    StepFunWebProvider,
    WebChatProvider,
    WebProviderSpec,
)

__all__ = [
    "ChatProvider",
    "DEFAULT_PROVIDER_ID",
    "DeepSeekWebProvider",
    "GlmWebProvider",
    "LocalOpenAIProvider",
    "MimoWebProvider",
    "PROVIDER_LABELS",
    "QwenWebProvider",
    "StepFunWebProvider",
    "WebChatProvider",
    "WebProviderSpec",
    "borrow_open_provider",
    "connect_existing_provider",
    "connect_fresh_provider_tab",
    "connect_provider",
    "provider_tab_availability",
    "provider_ids",
    "warm_provider_tabs",
]
