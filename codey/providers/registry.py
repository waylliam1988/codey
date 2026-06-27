from __future__ import annotations

from pathlib import Path

from codey.browser import DEFAULT_PORT, DEFAULT_PROFILE
from codey.providers.base import ChatProvider
from codey.providers.deepseek_web import DeepSeekWebProvider
from codey.providers.mimo_web import MimoWebProvider
from codey.providers.qwen_web import QwenWebProvider

DEFAULT_PROVIDER_ID = "deepseek"
PROVIDER_LABELS = {
    "deepseek": "DeepSeek",
    "mimo": "MiMo",
    "qwen": "Qwen",
}


def provider_ids() -> tuple[str, ...]:
    return tuple(PROVIDER_LABELS)


def connect_provider(
    provider_id: str,
    *,
    port: int = DEFAULT_PORT,
    profile: Path = DEFAULT_PROFILE,
) -> ChatProvider:
    normalized = (provider_id or DEFAULT_PROVIDER_ID).strip().lower()
    if normalized == "deepseek":
        return DeepSeekWebProvider.connect(port=port, profile=profile)
    if normalized == "qwen":
        return QwenWebProvider.connect(port=port, profile=profile)
    if normalized == "mimo":
        return MimoWebProvider.connect(port=port, profile=profile)
    raise ValueError(f"unsupported provider: {provider_id}")
