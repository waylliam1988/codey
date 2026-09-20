"""Single provider-registry entry point for the app layer.

``context.py`` and ``services.py`` historically each carried their own
``_provider_registry()`` facade to stay Playwright-free on import. This module
owns that facade once; both callers delegate so mocks on the old attribute
paths keep working while new code imports here directly.

Import cost: this module is light (catalog labels + lazy registry import).
The Playwright-backed registry loads only inside the functions below.
"""

from __future__ import annotations

import threading
import time as _time
from typing import Any

from codey.providers import controls as provider_controls  # noqa: F401  (re-export for probes)
from codey.providers.catalog import DEFAULT_PROVIDER_ID, PROVIDER_LABELS  # noqa: F401
from codey.providers.capabilities import rank_providers


def _provider_registry():
    """Import the connection registry on first use, never on module import."""
    from codey.providers import registry as _registry

    return _registry


def provider_tab_availability() -> dict:
    return _provider_registry().provider_tab_availability()


def warm_provider_tabs(*args: object, **kwargs: object) -> dict:
    return _provider_registry().warm_provider_tabs(*args, **kwargs)


def connect_provider(*args: object, **kwargs: object) -> object:
    return _provider_registry().connect_provider(*args, **kwargs)


def connect_existing_provider(provider_id: str) -> object:
    return _provider_registry().connect_existing_provider(provider_id)


def connect_fresh_provider_tab(provider_id: str, **kwargs: object) -> object:
    return _provider_registry().connect_fresh_provider_tab(provider_id, **kwargs)


def borrow_open_provider(provider_id: str, owner_page: object) -> object | None:
    return _provider_registry().borrow_open_provider(provider_id, owner_page)


def reviewer_candidates(
    ctx: Any,
    writer_id: str,
    *,
    supervisor: object | None = None,
) -> tuple[str, ...]:
    writer = (writer_id or DEFAULT_PROVIDER_ID).strip().lower()
    if supervisor is None:
        supervisor = ctx.providers.supervisor
    candidates = tuple(
        provider_id
        for provider_id in PROVIDER_LABELS
        if provider_id != writer
        and provider_id != "local"
        and supervisor.is_available(provider_id)
    )
    return rank_providers(candidates, mode="review")


PROVIDER_AVAILABILITY_TTL_S = 3.0
_AVAIL_LOCK = threading.Lock()
_AVAIL_STATUSES: dict[str, bool] = {}
_AVAIL_AT: list[float] = [0.0]


def _note_availability_statuses(raw: dict[str, bool], now: float) -> None:
    with _AVAIL_LOCK:
        _AVAIL_STATUSES.clear()
        _AVAIL_STATUSES.update(raw)
        _AVAIL_AT[0] = now


def reset_provider_availability_cache() -> None:
    """Clear the CDP availability cache; tests call this to avoid cross-test bleed."""
    _note_availability_statuses({}, 0.0)


def provider_availability(ctx: Any) -> dict[str, bool]:
    now = _time.monotonic()
    with _AVAIL_LOCK:
        cached_at = _AVAIL_AT[0]
        cached = dict(_AVAIL_STATUSES)
    if cached and now - cached_at < PROVIDER_AVAILABILITY_TTL_S:
        return provider_availability_from_statuses(ctx, cached)
    raw = provider_tab_availability()
    _note_availability_statuses(raw, now)
    return provider_availability_from_statuses(ctx, raw)


def provider_availability_from_statuses(
    ctx: Any,
    statuses: dict[str, bool],
) -> dict[str, bool]:
    supervisor = ctx.providers.supervisor
    return {
        provider_id: available and supervisor.is_available(provider_id)
        for provider_id, available in statuses.items()
    }


def provider_payload(statuses: dict[str, bool] | None = None) -> list[dict]:
    statuses = statuses or {}
    return [
        {"id": provider_id, "label": label, "available": bool(statuses.get(provider_id))}
        for provider_id, label in PROVIDER_LABELS.items()
    ]


def provider_catalog() -> list[dict]:
    """Cheap static catalog: ids + labels only, never probes CDP or network."""
    return [
        {"id": provider_id, "label": label}
        for provider_id, label in PROVIDER_LABELS.items()
    ]


def provider_status_update(provider_id: str, available: bool) -> list[dict]:
    return [{
        "id": provider_id,
        "label": PROVIDER_LABELS.get(provider_id, provider_id),
        "available": available,
    }]


__all__ = [
    "PROVIDER_AVAILABILITY_TTL_S",
    "borrow_open_provider",
    "connect_existing_provider",
    "connect_fresh_provider_tab",
    "connect_provider",
    "provider_availability",
    "provider_availability_from_statuses",
    "provider_catalog",
    "provider_payload",
    "provider_status_update",
    "provider_tab_availability",
    "reset_provider_availability_cache",
    "reviewer_candidates",
    "warm_provider_tabs",
]
