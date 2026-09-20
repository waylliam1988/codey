"""Single provider-registry entry point for the app layer.

Provider connection, availability, labels, and tab warmup live here. Callers
across ``app/`` import this module directly; there is exactly one facade.

Import cost: this module is light (catalog labels + lazy registry import).
The Playwright-backed registry loads only inside the functions below.
"""

from __future__ import annotations

import threading
import time as _time

from codey.automation.browser_worker import submit as submit_browser_task
from codey.operations.task_state import TaskState
from codey.providers.capabilities import rank_providers
from codey.providers.catalog import DEFAULT_PROVIDER_ID, PROVIDER_LABELS  # noqa: F401
from codey.utils.refs import clip, digest_text


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
    ctx: TaskState,
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


def provider_availability(ctx: TaskState) -> dict[str, bool]:
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
    ctx: TaskState,
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


def review_label(provider_id: str) -> str:
    return PROVIDER_LABELS.get(provider_id, provider_id)


def provider_failover_order(providers) -> tuple[str, ...]:
    """Order providers by open tabs first, then registry order."""
    return providers.failover_order(provider_tab_availability)


def open_provider_session(ctx: TaskState, provider_id: str = DEFAULT_PROVIDER_ID):
    """Connect a provider and announce it on the session event stream."""
    ctx.set_run_status("connecting")
    ctx.emit({"type": "status", "status": "connecting"})
    provider = connect_provider(provider_id)
    ctx.set_run_status("running")
    ctx.emit({
        "type": "providers",
        "providers": provider_status_update(provider_id, True),
    })
    return provider


def run_provider_warmup(ctx: TaskState, runner=None) -> None:
    if runner is None:
        runner = warm_provider_tabs
    try:
        raw_statuses = runner()
        _note_availability_statuses(dict(raw_statuses), _time.monotonic())
        statuses = provider_availability_from_statuses(ctx, raw_statuses)
        ctx.emit({"type": "providers", "providers": provider_payload(statuses)})
    except Exception as exc:
        text = f"{type(exc).__name__}: {exc}"
        try:
            ctx.emit({
                "type": "status",
                "status": "Provider warmup failed",
                "detail": clip(text, 240),
                "error_ref": digest_text(text)[:24],
            })
        except Exception:
            return


def start_provider_warmup(ctx: TaskState, runner=None, *, delay_s: float = 0.0) -> None:
    """Queue warmup; serve() passes a short delay to stay off the boot path."""
    import threading as _threading

    def _delayed() -> None:
        submit_browser_task(run_provider_warmup, ctx, runner)

    if delay_s <= 0:
        _delayed()
        return
    timer = _threading.Timer(delay_s, _delayed)
    timer.daemon = True
    timer.start()


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
    "open_provider_session",
    "provider_failover_order",
    "provider_status_update",
    "provider_tab_availability",
    "reset_provider_availability_cache",
    "review_label",
    "reviewer_candidates",
    "run_provider_warmup",
    "start_provider_warmup",
    "warm_provider_tabs",
]
