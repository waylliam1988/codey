from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codey.adapter_overrides import load_enabled_override
from codey.browser import (
    DEFAULT_PORT,
    DEFAULT_PROFILE,
    PROVIDER_URL_CONTAINS,
    detect_open_provider_tabs,
    warm_provider_tabs as browser_warm_provider_tabs,
)
from codey.providers.base import ChatProvider
from codey.providers.deepseek_web import DeepSeekWebProvider
from codey.providers.glm_web import GlmWebProvider
from codey.providers.local_openai import LocalOpenAIProvider, local_endpoint_available
from codey.providers.mimo_web import MimoWebProvider
from codey.providers.qwen_web import QwenWebProvider
from codey.provider_worker import WorkerChatProvider

DEFAULT_PROVIDER_ID = "deepseek"
PROVIDER_LABELS = {
    "deepseek": "DeepSeek",
    "mimo": "MiMo",
    "qwen": "Qwen",
    "glm": "GLM",
    "local": "Local",
}
WEB_PROVIDER_LABELS = {
    key: label for key, label in PROVIDER_LABELS.items() if key != "local"
}
PROVIDER_TYPES = {
    "deepseek": DeepSeekWebProvider,
    "mimo": MimoWebProvider,
    "qwen": QwenWebProvider,
    "glm": GlmWebProvider,
    "local": LocalOpenAIProvider,
}
PROVIDER_WORKER_PORT_OFFSETS = {
    "deepseek": 101,
    "mimo": 102,
    "qwen": 103,
    "glm": 104,
}
WORKER_CHILD_ENV = "CODEY_PROVIDER_WORKER_CHILD"


@dataclass
class _BorrowedSession:
    page: Any

    def close(self) -> None:
        """The owning provider connection keeps this Playwright context alive."""


def provider_ids() -> tuple[str, ...]:
    return tuple(PROVIDER_LABELS)


def provider_tab_availability() -> dict[str, bool]:
    statuses = detect_open_provider_tabs()
    payload = {provider_id: bool(statuses.get(provider_id)) for provider_id in WEB_PROVIDER_LABELS}
    payload["local"] = local_endpoint_available()
    return payload


def warm_provider_tabs(
    *,
    port: int = DEFAULT_PORT,
    profile: Path = DEFAULT_PROFILE,
) -> dict[str, bool]:
    statuses = browser_warm_provider_tabs(port=port, profile=profile)
    payload = {provider_id: bool(statuses.get(provider_id)) for provider_id in WEB_PROVIDER_LABELS}
    payload["local"] = local_endpoint_available()
    return payload


def connect_provider(
    provider_id: str,
    *,
    port: int = DEFAULT_PORT,
    profile: Path = DEFAULT_PROFILE,
    open_if_missing: bool = True,
    bring_to_front: bool = True,
) -> ChatProvider:
    normalized = (provider_id or DEFAULT_PROVIDER_ID).strip().lower()
    provider_type = PROVIDER_TYPES.get(normalized)
    if provider_type is None:
        raise ValueError(f"unsupported provider: {provider_id}")
    if normalized == "local":
        return provider_type.connect()
    if open_if_missing and os.environ.get(WORKER_CHILD_ENV) != "1":
        override = load_enabled_override(normalized)
        if override is not None:
            return WorkerChatProvider(
                normalized,
                override,
                port=port + PROVIDER_WORKER_PORT_OFFSETS.get(normalized, 100),
            )
    return provider_type.connect(
        port=port,
        profile=profile,
        open_if_missing=open_if_missing,
        bring_to_front=bring_to_front,
    )


def borrow_open_provider(provider_id: str, owner_page: Any) -> ChatProvider | None:
    """Wrap an already-open sibling tab without creating another CDP connection."""
    normalized = (provider_id or "").strip().lower()
    if normalized == "local":
        return None
    marker = PROVIDER_URL_CONTAINS.get(normalized)
    if not marker:
        return None
    try:
        pages = tuple(owner_page.context.pages)
    except Exception:
        return None
    page = next(
        (
            candidate
            for candidate in pages
            if candidate is not owner_page and marker in str(candidate.url or "")
        ),
        None,
    )
    if page is None:
        return None
    session = _BorrowedSession(page)
    provider_type = PROVIDER_TYPES.get(normalized)
    return provider_type(session) if provider_type is not None else None


def connect_existing_provider(provider_id: str) -> ChatProvider:
    """Attach to an already-open provider tab without opening a new one."""
    return connect_provider(
        provider_id,
        open_if_missing=False,
        bring_to_front=False,
    )


def connect_fresh_provider_tab(
    provider_id: str,
    *,
    port: int = DEFAULT_PORT,
    profile: Path = DEFAULT_PROFILE,
) -> ChatProvider:
    """Open a temporary provider tab for isolated review-style work."""

    normalized = (provider_id or DEFAULT_PROVIDER_ID).strip().lower()
    provider_type = PROVIDER_TYPES.get(normalized)
    if provider_type is None:
        raise ValueError(f"unsupported provider: {provider_id}")
    if normalized == "local":
        return provider_type.connect()
    if os.environ.get(WORKER_CHILD_ENV) != "1":
        override = load_enabled_override(normalized)
        if override is not None:
            return WorkerChatProvider(
                normalized,
                override,
                port=port + PROVIDER_WORKER_PORT_OFFSETS.get(normalized, 100),
            )
    return provider_type.connect(
        port=port,
        profile=profile,
        open_if_missing=True,
        bring_to_front=False,
        fresh_tab=True,
    )
