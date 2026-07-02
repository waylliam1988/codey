from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codey.browser import (
    DEFAULT_PORT,
    DEFAULT_PROFILE,
    PROVIDER_URL_CONTAINS,
    detect_open_provider_tabs,
)
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


@dataclass
class _BorrowedSession:
    page: Any

    def close(self) -> None:
        """The owning provider connection keeps this Playwright context alive."""


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


def borrow_open_provider(provider_id: str, owner_page: Any) -> ChatProvider | None:
    """Wrap an already-open sibling tab without creating another CDP connection."""
    normalized = (provider_id or "").strip().lower()
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
    if normalized == "deepseek":
        return DeepSeekWebProvider(session)
    if normalized == "qwen":
        return QwenWebProvider(session)
    if normalized == "mimo":
        return MimoWebProvider(session)
    return None


def connect_existing_provider(provider_id: str) -> ChatProvider:
    """Attach to an already-open provider tab without opening a new one."""
    return connect_provider(
        provider_id,
        open_if_missing=False,
        bring_to_front=False,
    )
