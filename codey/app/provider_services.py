"""Single provider-registry entry point for the app layer.

Provider connection, availability, labels, and tab warmup live here. Callers
across ``app/`` import this module directly; there is exactly one facade.

Import cost: this module is light (catalog labels + lazy registry import).
The Playwright-backed registry loads only inside the functions below.
"""

from __future__ import annotations

import threading
import time as _time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast

from codey.automation.browser_worker import submit as submit_browser_task
from codey.operations.task_state import TaskState
from codey.providers.base import ChatProvider
from codey.providers.capabilities import rank_providers
from codey.providers.catalog import API_CONNECTIONS, DEFAULT_PROVIDER_ID, PROVIDER_LABELS  # noqa: F401
from codey.providers.supervisor import ProviderSupervisor
from codey.utils.refs import clip, digest_text


def _provider_registry() -> Any:
    """Import the connection registry on first use, never on module import."""
    from codey.providers import registry as _registry

    return _registry


def provider_tab_availability(*, allowed: set[str] | None = None) -> dict[str, Any]:
    return cast(dict[str, Any], _provider_registry().provider_tab_availability(allowed=allowed))


def warm_provider_tabs(*args: object, **kwargs: object) -> dict[str, Any]:
    return cast(dict[str, Any], _provider_registry().warm_provider_tabs(*args, **kwargs))


def connect_provider(*args: object, **kwargs: object) -> ChatProvider:
    return cast(ChatProvider, _provider_registry().connect_provider(*args, **kwargs))


def connect_existing_provider(provider_id: str) -> ChatProvider:
    return cast(ChatProvider, _provider_registry().connect_existing_provider(provider_id))


def connect_fresh_provider_tab(provider_id: str, **kwargs: object) -> ChatProvider:
    return cast(ChatProvider, _provider_registry().connect_fresh_provider_tab(provider_id, **kwargs))


def borrow_open_provider(provider_id: str, owner_page: object) -> ChatProvider | None:
    return cast(ChatProvider | None, _provider_registry().borrow_open_provider(provider_id, owner_page))


def reviewer_candidates(
    ctx: TaskState,
    writer_id: str,
    *,
    supervisor: ProviderSupervisor | None = None,
) -> tuple[str, ...]:
    from codey.providers.ids import normalize_provider_id

    writer = normalize_provider_id(writer_id) or DEFAULT_PROVIDER_ID
    if supervisor is None:
        supervisor = ctx.providers.supervisor
    assert supervisor is not None
    candidates = tuple(
        provider_id
        for provider_id in PROVIDER_LABELS
        if provider_id != writer
        and provider_id not in API_CONNECTIONS
        and ctx.providers.model_preferences.allows(provider_id)
        and supervisor.is_available(provider_id)
    )
    return rank_providers(candidates, mode="review")


@contextmanager
def use_model(ctx: TaskState, provider_id: str, model_id: str = "") -> Iterator[None]:
    """Hold an exact model while a secondary operation owns it."""
    key = provider_id, model_id
    with ctx.lock:
        if not ctx.providers.model_preferences.allows(*key):
            raise ValueError("Selected model is disabled. Choose an enabled model in Settings.")
        ctx.providers.model_uses[key] += 1
    try:
        yield
    finally:
        with ctx.lock:
            ctx.providers.model_uses[key] -= 1
            if not ctx.providers.model_uses[key]:
                del ctx.providers.model_uses[key]


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


def recommended_default_provider(statuses: dict[str, bool] | None = None) -> str:
    """Cold-start default: keep the configured default when it is up.

    Otherwise prefer a reachable local model over nothing, then any open
    provider. Frontends apply this only when the user has no explicit
    selection (e.g. a brand-new session); it never overrides history.
    """
    available = {pid for pid, ok in (statuses or {}).items() if ok is True}
    if DEFAULT_PROVIDER_ID in available:
        return DEFAULT_PROVIDER_ID
    if "local" in available:
        return "local"
    for provider_id in PROVIDER_LABELS:
        if provider_id in available:
            return provider_id
    return DEFAULT_PROVIDER_ID


def provider_availability(ctx: TaskState) -> dict[str, bool]:
    allowed = {pid for pid in PROVIDER_LABELS if ctx.providers.model_preferences.allows(pid)}
    if not allowed:
        return {pid: False for pid in PROVIDER_LABELS}
    now = _time.monotonic()
    with _AVAIL_LOCK:
        cached_at = _AVAIL_AT[0]
        cached = dict(_AVAIL_STATUSES)
    if cached and now - cached_at < PROVIDER_AVAILABILITY_TTL_S:
        return provider_availability_from_statuses(ctx, cached)
    raw = provider_tab_availability(allowed=allowed)
    _note_availability_statuses(raw, now)
    return provider_availability_from_statuses(ctx, raw)


def provider_availability_from_statuses(
    ctx: TaskState,
    statuses: dict[str, bool],
) -> dict[str, bool]:
    supervisor = ctx.providers.supervisor
    return {
        provider_id: available and ctx.providers.model_preferences.allows(provider_id) and supervisor.is_available(provider_id)
        for provider_id, available in statuses.items()
    }


def provider_payload(statuses: dict[str, bool] | None = None) -> list[dict[str, Any]]:
    statuses = statuses or {}
    return [
        {"id": provider_id, "label": label, "available": statuses.get(provider_id) is True}
        for provider_id, label in PROVIDER_LABELS.items()
    ]


def provider_catalog() -> list[dict[str, Any]]:
    """Cheap static catalog: ids + labels only, never probes CDP or network."""
    return [
        {"id": provider_id, "label": label}
        for provider_id, label in PROVIDER_LABELS.items()
    ]


def provider_status_update(provider_id: str, available: bool) -> list[dict[str, Any]]:
    return [{
        "id": provider_id,
        "label": PROVIDER_LABELS.get(provider_id, provider_id),
        "available": available,
    }]


def review_label(provider_id: str) -> str:
    return PROVIDER_LABELS.get(provider_id, provider_id)


def provider_failover_order(providers: Any) -> tuple[str, ...]:
    """Order providers by open tabs first, then registry order."""
    allowed = {pid for pid in PROVIDER_LABELS if pid not in API_CONNECTIONS and providers.model_preferences.allows(pid)}
    return cast(tuple[str, ...], providers.failover_order(lambda: provider_tab_availability(allowed=allowed)))


def open_provider_session(ctx: TaskState, provider_id: str = DEFAULT_PROVIDER_ID) -> Any:
    """Connect a provider and announce it on the session event stream."""
    run = ctx.current_run()
    selection = ctx.run_registry.api_selection_for(run.run_id) if run is not None else None
    if not ctx.providers.model_preferences.allows(provider_id, getattr(selection, "model_id", "")):
        raise ValueError("Selected model is disabled. Choose an enabled model in Settings.")
    ctx.set_run_status("connecting")
    ctx.emit({"type": "status", "status": "connecting"})
    from codey.providers.api_connections import open_selection

    provider = open_selection(selection) if selection is not None and selection.connection_id == provider_id else connect_provider(provider_id)
    ctx.set_run_status("running")
    ctx.emit({"type": "status", "status": "running"})
    ctx.emit({
        "type": "providers",
        "providers": provider_status_update(provider_id, True),
    })
    return provider


def run_provider_warmup(ctx: TaskState, runner: Any = None) -> None:
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


def start_provider_warmup(ctx: TaskState, runner: Any = None, *, delay_s: float = 0.0) -> None:
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
    "recommended_default_provider",
    "reset_provider_availability_cache",
    "review_label",
    "reviewer_candidates",
    "run_provider_warmup",
    "start_provider_warmup",
    "warm_provider_tabs",
]
