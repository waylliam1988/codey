from __future__ import annotations

from pathlib import Path

from codey.browser import DEFAULT_PORT, DEFAULT_PROFILE, detect_open_provider_tabs
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


def provider_tab_availability() -> dict[str, bool]:
    statuses = detect_open_provider_tabs()
    return {provider_id: bool(statuses.get(provider_id)) for provider_id in PROVIDER_LABELS}


def connect_provider(
    provider_id: str,
    *,
    port: int = DEFAULT_PORT,
    profile: Path = DEFAULT_PROFILE,
    open_if_missing: bool = True,
    bring_to_front: bool = True,
) -> ChatProvider:
    normalized = (provider_id or DEFAULT_PROVIDER_ID).strip().lower()
    if normalized == "deepseek":
        return DeepSeekWebProvider.connect(
            port=port,
            profile=profile,
            open_if_missing=open_if_missing,
            bring_to_front=bring_to_front,
        )
    if normalized == "qwen":
        return QwenWebProvider.connect(
            port=port,
            profile=profile,
            open_if_missing=open_if_missing,
            bring_to_front=bring_to_front,
        )
    if normalized == "mimo":
        return MimoWebProvider.connect(
            port=port,
            profile=profile,
            open_if_missing=open_if_missing,
            bring_to_front=bring_to_front,
        )
    raise ValueError(f"unsupported provider: {provider_id}")


def connect_existing_provider(provider_id: str) -> ChatProvider:
    """Attach to an already-open provider tab without opening a new one."""
    return connect_provider(
        provider_id,
        open_if_missing=False,
        bring_to_front=False,
    )
