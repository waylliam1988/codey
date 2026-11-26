"""Task-local provider control state and atomic send recovery transactions."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar, cast

if TYPE_CHECKING:
    from playwright.sync_api import Locator
from codey.providers import flow as provider_flow
from codey.providers import revival as provider_revival
from codey.providers.control_fingerprint import _host_matches, _page_host
from codey.providers.control_store import CONTROL_STORE, load_control, save_control
from codey.providers.diagnostics import ResponseMissing
from codey.providers.profiles import get_profile

logger = logging.getLogger(__name__)
_P = ParamSpec("_P")
_R = TypeVar("_R")
_context = threading.local()
_TASK_CONTEXT_FIELDS = (
    "session_id",
    "doctor_attempts",
    "sources",
    "pending",
    "response_locators",
    "response_watches",
    "revival_attempts",
    "flow_cache",
)


@dataclass
class _RevivalSend:
    host: str
    staged: dict[str, dict[str, Any]]
    verified: set[str]
    learned_verified: set[str]
    profile_hash: str
    flow_recipe: dict[str, tuple[str, ...]] | None = None
    staged_flow: dict[str, tuple[str, ...]] | None = None
    learned_flow_verified: bool = False
    loaded_flow_stages: set[str] = field(default_factory=set)
    flow_evaluated_stages: set[str] = field(default_factory=set)
    flow_used_stages: set[str] = field(default_factory=set)
    persistent_failure_recorded: bool = False
    path: Path | None = None


@dataclass(frozen=True)
class _PendingControl:
    page: Any
    fingerprint: dict[str, Any]
    locator: Locator | None = None


def begin_task_context(session_id: str) -> None:
    end_task_context()
    _context.session_id = session_id or ""
    _context.doctor_attempts = set()
    provider_flow.begin_task_context(session_id)


def end_task_context() -> None:
    for name in _TASK_CONTEXT_FIELDS:
        if hasattr(_context, name):
            delattr(_context, name)
    provider_flow.end_task_context()


def revival_send(provider_id: str) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]:
    """Wrap one provider send in an atomic local-control recovery transaction."""
    def decorate(func: Callable[_P, _R]) -> Callable[_P, _R]:
        @wraps(func)
        def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            page = cast(Any, args[0]) if args else kwargs.get("page")
            _begin_revival_send(provider_id, page)
            try:
                result = func(*args, **kwargs)
            except BaseException as exc:
                _record_revival_flow_failure(provider_id, exc)
                _abort_revival_send(provider_id)
                raise
            _complete_revival_send(provider_id)
            return result
        return wrapped
    return decorate


def _doctor_attempts() -> set[tuple[str, str, str]]:
    attempts = getattr(_context, "doctor_attempts", None)
    if attempts is None:
        attempts = set()
        _context.doctor_attempts = attempts
    return attempts


def confirm_control(provider_id: str, action: str, *, path: Path | None = None) -> None:
    """Promote the learned control only after the caller observed the expected state."""
    source = _source_for(provider_id, action)
    if source == "pending":
        pending = _pending_map().get((provider_id, action))
        if pending is None:
            return
        page = pending.page
        fingerprint = pending.fingerprint
        attempt = _revival_attempts().get(provider_id)
        if attempt is not None:
            attempt.staged[action] = fingerprint
            attempt.verified.add(action)
            attempt.path = path or attempt.path
            _remember_source(provider_id, action, "staged")
            return
        _pending_map().pop((provider_id, action), None)
        try:
            save_control(
                provider_id,
                action,
                page,
                fingerprint,
                path=path,
                verified=True,
            )
        except OSError:
            _remember_source(provider_id, action, "")
            return
        _remember_source(provider_id, action, "learned")
        return
    if source != "learned":
        return
    attempt = _revival_attempts().get(provider_id)
    if attempt is not None:
        attempt.verified.add(action)
        attempt.learned_verified.add(action)
        attempt.path = path or attempt.path
        return
    _update_learned_control(provider_id, action, success=True, path=path)


def reject_control(
    provider_id: str,
    action: str,
    *,
    path: Path | None = None,
    page: Any | None = None,
) -> None:
    """Forget a learned control after repeated failed state validation."""
    source = _source_for(provider_id, action)
    if source == "staged":
        attempt = _revival_attempts().get(provider_id)
        if attempt is not None:
            attempt.staged.pop(action, None)
            attempt.verified.discard(action)
        _pending_map().pop((provider_id, action), None)
        _remember_source(provider_id, action, "")
        return
    if source == "pending":
        _pending_map().pop((provider_id, action), None)
        _remember_source(provider_id, action, "")
        return
    if source == "learned":
        _update_learned_control(provider_id, action, success=False, path=path)
        return
    if source or page is None:
        return
    record = load_control(provider_id, action, path=path)
    if not isinstance(record, dict):
        return
    if not _host_matches(_page_host(page), str(record.get("host") or "")):
        return
    _update_learned_control(provider_id, action, success=False, path=path)


def _update_learned_control(
    provider_id: str,
    action: str,
    *,
    success: bool,
    path: Path | None,
) -> None:
    store = path or CONTROL_STORE
    try:
        if success:
            changed = provider_revival.record_control_success(
                store,
                provider_id,
                action,
            )
        else:
            changed = provider_revival.record_control_failure(
                store,
                provider_id,
                action,
            )
        if changed:
            _invalidate_flow_cache(store, provider_id)
            if not success:
                attempt = _revival_attempts().get(provider_id)
                if attempt is not None:
                    attempt.persistent_failure_recorded = True
    except (OSError, ValueError) as exc:
        logger.warning("Failed to record learned control update for %s: %s", provider_id, exc)
        return


def _source_map() -> dict[tuple[str, str], str]:
    sources = getattr(_context, "sources", None)
    if sources is None:
        sources = dict[tuple[str, str], str]()
        _context.sources = sources
    return sources


def _pending_map() -> dict[tuple[str, str], _PendingControl]:
    pending = getattr(_context, "pending", None)
    if pending is None:
        pending = dict[tuple[str, str], _PendingControl]()
        _context.pending = pending
    return pending


def _response_locator_map() -> dict[str, Locator | None]:
    locators: dict[str, Locator | None] | None = getattr(_context, "response_locators", None)
    if locators is None:
        locators = {}
        _context.response_locators = locators
    return locators


def _revival_attempts() -> dict[str, _RevivalSend]:
    attempts = getattr(_context, "revival_attempts", None)
    if attempts is None:
        attempts = dict[str, _RevivalSend]()
        _context.revival_attempts = attempts
    return attempts


def _flow_cache() -> dict[tuple[str, str, str], dict[str, tuple[str, ...]] | None]:
    cache = getattr(_context, "flow_cache", None)
    if cache is None:
        cache = dict[tuple[str, str, str], dict[str, tuple[str, ...]] | None]()
        _context.flow_cache = cache
    return cache


def _flow_cache_key(
    path: Path,
    provider_id: str,
    profile_digest: str,
) -> tuple[str, str, str]:
    try:
        store = str(path.resolve())
    except OSError:
        store = str(path.absolute())
    return provider_id, profile_digest, store


def _load_flow_cached(
    path: Path,
    provider_id: str,
    profile_digest: str,
) -> dict[str, tuple[str, ...]] | None:
    key = _flow_cache_key(path, provider_id, profile_digest)
    cache = _flow_cache()
    if key not in cache:
        cache[key] = provider_revival.load_flow_recipe(
            path,
            provider_id,
            profile_digest,
        )
    return cache[key]


def _invalidate_flow_cache(path: Path, provider_id: str) -> None:
    target = _flow_cache_key(path, provider_id, "")[2]
    for key in tuple(_flow_cache()):
        if key[0] == provider_id and key[2] == target:
            _flow_cache().pop(key, None)


def _begin_revival_send(provider_id: str, page: Any) -> None:
    _abort_revival_send(provider_id)
    profile_digest = provider_flow.profile_hash(get_profile(provider_id))
    try:
        flow_recipe = _load_flow_cached(CONTROL_STORE, provider_id, profile_digest)
    except (OSError, ValueError) as exc:
        logger.warning("Failed to load revival flow for %s: %s", provider_id, exc)
        flow_recipe = None
    _revival_attempts()[provider_id] = _RevivalSend(
        host=_page_host(page),
        staged={},
        verified=set(),
        learned_verified=set(),
        profile_hash=profile_digest,
        flow_recipe=flow_recipe,
        loaded_flow_stages=set(flow_recipe or {}),
    )


def _abort_revival_send(provider_id: str) -> None:
    _revival_attempts().pop(provider_id, None)
    for key in tuple(_pending_map()):
        if key[0] == provider_id:
            _pending_map().pop(key, None)
            if _source_for(*key) in {"pending", "staged"}:
                _remember_source(*key, "")
    for key, source in tuple(_source_map().items()):
        if key[0] == provider_id and source in {"pending", "staged"}:
            _remember_source(*key, "")


def _complete_revival_send(provider_id: str) -> None:
    attempt = _revival_attempts().pop(provider_id, None)
    if attempt is None:
        return
    try:
        path = attempt.path or CONTROL_STORE
        changed = provider_revival.complete_send(
            path,
            provider_id,
            attempt.host,
            attempt.staged,
            attempt.verified,
            attempt.learned_verified,
            staged_flow=attempt.staged_flow,
            learned_flow_verified=attempt.learned_flow_verified,
            built_in_profile_hash=attempt.profile_hash,
        )
        if changed:
            _invalidate_flow_cache(path, provider_id)
    except (OSError, ValueError) as exc:
        logger.warning("Failed to complete revival send for %s: %s", provider_id, exc)
        for action in attempt.staged:
            _remember_source(provider_id, action, "")
        _clear_pending_controls(provider_id)
        return
    _clear_pending_controls(provider_id)
    for action in attempt.staged:
        _remember_source(provider_id, action, "learned")


def flow_matches(
    provider_id: str,
    stage: str,
    observation: provider_flow.FlowObservation,
    trace: provider_flow.FlowTrace | None = None,
    *,
    mark_used: bool = True,
) -> bool:
    attempt = _revival_attempts().get(provider_id)
    if attempt is None or attempt.flow_recipe is None:
        return False
    if stage not in attempt.flow_recipe:
        return False
    if stage in attempt.loaded_flow_stages:
        attempt.flow_evaluated_stages.add(stage)
    matched = provider_flow.evaluate(attempt.flow_recipe, stage, observation, trace)
    if matched:
        attempt.learned_flow_verified = True
        if mark_used and stage in attempt.loaded_flow_stages:
            attempt.flow_used_stages.add(stage)
    return matched


def flow_stage_ready(
    page: Any,
    provider_id: str,
    stage: str,
    trace: provider_flow.FlowTrace,
    observation: provider_flow.FlowObservation,
    *,
    built_in_ready: bool,
    allow_recovery: bool = False,
) -> bool:
    """Evaluate built-in state first, then a verified or recoverable recipe."""
    if built_in_ready:
        flow_matches(
            provider_id,
            stage,
            observation,
            trace,
            mark_used=False,
        )
        return True
    if flow_matches(provider_id, stage, observation, trace):
        return True
    return allow_recovery and recover_flow(page, provider_id, stage, trace)


def read_flow_response(
    provider_id: str,
    stage: str,
    reader: Callable[[], str],
) -> str:
    """Type an unreadable final answer only when a loaded Flow chose completion."""
    try:
        return reader()
    except RuntimeError as exc:
        attempt = _revival_attempts().get(provider_id)
        if attempt is not None and stage in attempt.flow_used_stages:
            raise ResponseMissing(str(exc), stage=stage) from exc
        raise


def recover_flow(
    page: Any,
    provider_id: str,
    stage: str,
    trace: provider_flow.FlowTrace,
) -> bool:
    """Stage one model-selected recipe only when current boolean facts prove it."""
    attempt = _revival_attempts().get(provider_id)
    if attempt is None:
        return False
    recipe = provider_flow.request_recovery(provider_id, stage, trace, page)
    if recipe is None or not provider_flow.evaluate(
        recipe,
        stage,
        trace.latest(),
        trace,
    ):
        return False
    attempt.staged_flow = recipe
    attempt.flow_recipe = recipe
    attempt.learned_flow_verified = False
    return True


def _record_revival_flow_failure(provider_id: str, error: BaseException) -> None:
    attempt = _revival_attempts().get(provider_id)
    if (
        attempt is None
        or attempt.staged_flow is not None
        or attempt.persistent_failure_recorded
    ):
        return
    kind = str(getattr(error, "provider_failure_kind", "") or "")
    stage = str(getattr(error, "provider_failure_stage", "") or "")
    if (
        kind not in provider_flow.RECOVERABLE_FAILURE_KINDS
        or stage not in attempt.loaded_flow_stages
        or stage not in attempt.flow_evaluated_stages
    ):
        return
    store = attempt.path or CONTROL_STORE
    try:
        if provider_revival.record_flow_failure(store, provider_id):
            _invalidate_flow_cache(store, provider_id)
    except (OSError, ValueError) as exc:
        logger.warning("Failed to record revival flow failure for %s: %s", provider_id, exc)


def _clear_pending_controls(provider_id: str) -> None:
    for key in tuple(_pending_map()):
        if key[0] == provider_id:
            _pending_map().pop(key, None)


def _remember_pending(
    provider_id: str,
    action: str,
    page: Any,
    fingerprint: dict[str, Any],
    locator: Locator | None = None,
) -> None:
    _pending_map()[(provider_id, action)] = _PendingControl(
        page,
        fingerprint,
        locator,
    )


def _remember_source(provider_id: str, action: str, source: str) -> None:
    _source_map()[(provider_id, action)] = source


def _source_for(provider_id: str, action: str) -> str:
    return _source_map().get((provider_id, action), "")
