"""Chat provider adapters."""

from codey.providers.base import ChatProvider
from codey.providers.deepseek_web import DeepSeekWebProvider
from codey.providers.qwen_web import QwenWebProvider
from codey.providers.registry import (
    DEFAULT_PROVIDER_ID,
    PROVIDER_LABELS,
    connect_provider,
    provider_ids,
)

__all__ = [
    "ChatProvider",
    "DEFAULT_PROVIDER_ID",
    "DeepSeekWebProvider",
    "PROVIDER_LABELS",
    "QwenWebProvider",
    "connect_provider",
    "provider_ids",
]
