"""Chat provider adapters."""

from codey.providers.base import ChatProvider
from codey.providers.deepseek_web import DeepSeekWebProvider
from codey.providers.glm_web import GlmWebProvider
from codey.providers.local_openai import LocalOpenAIProvider
from codey.providers.mimo_web import MimoWebProvider
from codey.providers.qwen_web import QwenWebProvider
from codey.providers.stepfun_web import StepFunWebProvider
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
    "borrow_open_provider",
    "connect_existing_provider",
    "connect_fresh_provider_tab",
    "connect_provider",
    "provider_tab_availability",
    "provider_ids",
    "warm_provider_tabs",
]
