"""Small self-repair layer for provider page controls.

Provider adapters should keep their normal selectors first.  This module is a
fallback: when a site moves the message box or send button, Codey can ask the
user to click the control once and keep only that latest teaching.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from codey.runtime import cancellation
from codey.providers import profile_doctor
from codey.providers import discovery as discovery
from codey.providers import flow as provider_flow
from codey.providers import revival as provider_revival
from codey.storage.local_store import DEFAULT_STATE_HOME, read_json, write_json_atomic
from codey.providers.diagnostics import ResponseMissing
from codey.providers.profiles import get_profile

logger = logging.getLogger(__name__)

CONTROL_MESSAGE_BOX = "message_box"
CONTROL_SEND_BUTTON = "send_button"
CONTROL_RESPONSE = "response"
CONTROL_LABELS = {
    CONTROL_MESSAGE_BOX: "message box",
    CONTROL_SEND_BUTTON: "send button",
    CONTROL_RESPONSE: "answer",
}
CONTROL_STORE = DEFAULT_STATE_HOME / "provider-controls.json"
_handler: Callable[["ControlTeachRequest"], Any] | None = None
_doctor_handler: Callable[[profile_doctor.ProfileDoctorRequest], str | None] | None = None
_context = threading.local()
_TASK_CONTEXT_FIELDS = (
    "session_id",
    "doctor_attempts",
    "assistance_depth",
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
    locator: Any | None = None


class ControlTeachCancelled(cancellation.TaskCancelled):
    """Raised when the user stops a paused control teaching request."""


@dataclass(frozen=True)
class CapturedControl:
    fingerprint: dict[str, Any]
    current_selector: str


@dataclass(frozen=True)
class ControlTeachRequest:
    provider_id: str
    action: str
    page: Any
    session_id: str = ""
    require_enabled: bool = False

    @property
    def message(self) -> str:
        label = CONTROL_LABELS.get(self.action, "control")
        return f"Click the {label} in the model page"


def set_teach_handler(handler: Callable[[ControlTeachRequest], Any] | None) -> None:
    global _handler
    _handler = handler


def set_doctor_handler(
    handler: Callable[[profile_doctor.ProfileDoctorRequest], str | None] | None,
) -> None:
    global _doctor_handler
    _doctor_handler = handler


def can_teach() -> bool:
    return _handler is not None and not _assistance_suppressed()


def can_doctor() -> bool:
    return _doctor_handler is not None and not _assistance_suppressed()


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


def revival_send(provider_id: str):
    """Wrap one provider send in an atomic local-control recovery transaction."""
    def decorate(func):
        @wraps(func)
        def wrapped(page, *args, **kwargs):
            _begin_revival_send(provider_id, page)
            try:
                result = func(page, *args, **kwargs)
            except BaseException as exc:
                _record_revival_flow_failure(provider_id, exc)
                _abort_revival_send(provider_id)
                raise
            _complete_revival_send(provider_id)
            return result
        return wrapped
    return decorate


def visible_locator(page: Any, selector: str) -> Any | None:
    locator = page.locator(selector)
    try:
        count = locator.count()
    except cancellation.TaskCancelled:
        raise
    except Exception:
        return None
    try:
        count = int(count)
    except (TypeError, ValueError):
        return None
    for index in range(count - 1, -1, -1):
        candidate = locator.nth(index)
        try:
            if candidate.is_visible():
                return candidate
        except Exception:
            continue
    return None


def locate_control(
    page: Any,
    provider_id: str,
    action: str,
    selectors: list[str] | tuple[str, ...],
    *,
    timeout: float = 0.0,
    require_enabled: bool = False,
    teach: bool = False,
    anchor: Any | None = None,
) -> Any | None:
    cancellation.check()
    deadline = time.time() + max(0.0, timeout)
    first = True
    while first or time.time() < deadline:
        cancellation.check()
        first = False
        for selector in selectors:
            control = visible_locator(page, selector)
            if _usable(control, require_enabled):
                _remember_source(provider_id, action, "profile")
                return control
        pending_source = _source_for(provider_id, action)
        if pending_source in {"pending", "staged"}:
            control = pending_control(
                page,
                provider_id,
                action,
                require_enabled=require_enabled,
            )
            if control is not None:
                _remember_source(provider_id, action, pending_source)
                return control
        control = saved_control(page, provider_id, action, require_enabled=require_enabled)
        if control is not None:
            _remember_source(provider_id, action, "learned")
            return control
        if time.time() >= deadline:
            break
        cancellation.wait(0.2)
    control = discover_control(
        page,
        provider_id,
        action,
        require_enabled=require_enabled,
        anchor=anchor,
    )
    if control is not None:
        return control
    if teach and can_teach():
        return request_teaching(
            page,
            provider_id,
            action,
            require_enabled=require_enabled,
        )
    return None


def pending_control(
    page: Any,
    provider_id: str,
    action: str,
    *,
    require_enabled: bool = False,
) -> Any | None:
    """Reuse one uncommitted control only within its originating page."""
    pending = _pending_map().get((provider_id, action))
    if pending is None or pending.page is not page:
        return None
    if pending.locator is not None:
        try:
            if pending.locator.is_visible() and _usable(
                pending.locator,
                require_enabled,
            ):
                return pending.locator
        except Exception:
            pass
    for selector in selector_candidates(pending.fingerprint, action):
        try:
            control = unique_visible_locator(page, selector)
        except Exception:
            continue
        if _usable(control, require_enabled):
            return control
    return None


def saved_control(
    page: Any,
    provider_id: str,
    action: str,
    *,
    require_enabled: bool = False,
) -> Any | None:
    record = load_control(provider_id, action)
    if not record:
        return None
    if not _host_matches(_page_host(page), str(record.get("host") or "")):
        return None
    fingerprint = record.get("fingerprint")
    if not isinstance(fingerprint, dict):
        return None
    for selector in selector_candidates(fingerprint, action):
        try:
            control = unique_visible_locator(page, selector)
        except Exception:
            continue
        if _usable(control, require_enabled):
            return control
    return None


def unique_visible_locator(page: Any, selector: str) -> Any | None:
    """Return a visible match only when the learned selector is unambiguous."""
    locator = page.locator(selector)
    matches: list[Any] = []
    try:
        count = int(locator.count())
    except Exception:
        return None
    for index in range(count):
        candidate = locator.nth(index)
        try:
            if candidate.is_visible():
                matches.append(candidate)
        except Exception:
            continue
    return matches[0] if len(matches) == 1 else None


def discover_control(
    page: Any,
    provider_id: str,
    action: str,
    *,
    require_enabled: bool = False,
    anchor: Any | None = None,
) -> Any | None:
    """Find a plausible composer control without allowing a broad selector to click it."""
    cancellation.check()
    if not _provider_host_allowed(page, provider_id):
        return None
    candidates = discovery.control_candidates(page, action, anchor=anchor)
    found = discovery.select_control_candidate(candidates, action)
    if found is not None and not _discovery_is_usable(found, action, require_enabled):
        found = None
    if found is None:
        found = _doctor_selection(
            page,
            provider_id,
            action,
            candidates,
            require_enabled=require_enabled,
        )
    if found is None:
        return None
    fingerprint = fingerprint_from_click(found.fingerprint)
    if not control_fingerprint_is_valid(fingerprint, action):
        return None
    control = found.locator
    if not _usable(control, require_enabled):
        return None
    _remember_pending(provider_id, action, page, fingerprint, control)
    _remember_source(provider_id, action, "pending")
    return control


def score_control_candidate(
    candidate: dict[str, Any],
    action: str,
    anchor_box: dict[str, Any] | None = None,
) -> int:
    return discovery.score_control_candidate(candidate, action, anchor_box)


def start_response_watch(page: Any, provider_id: str) -> None:
    """Observe only DOM changes made after the next message is submitted."""
    token = discovery.start_response_watch(page)
    if not token:
        return
    watches = getattr(_context, "response_watches", None)
    if watches is None:
        watches = {}
        _context.response_watches = watches
    watches[provider_id] = token
    _response_locator_map().pop(provider_id, None)


def stop_response_watch(page: Any, provider_id: str) -> None:
    watches = getattr(_context, "response_watches", {})
    token = watches.pop(provider_id, "") if isinstance(watches, dict) else ""
    discovery.stop_response_watch(page, token)
    _response_locator_map().pop(provider_id, None)


def locate_response(
    page: Any,
    provider_id: str,
    selectors: list[str] | tuple[str, ...],
) -> Any | None:
    """Locate the newest answer, with DOM-delta discovery as a bounded fallback."""
    for selector in selectors:
        response = visible_locator(page, selector)
        if response is not None:
            _remember_source(provider_id, CONTROL_RESPONSE, "profile")
            return response
    cached = _response_locator_map().get(provider_id)
    if cached is not None:
        try:
            if cached.is_visible():
                return cached
        except Exception:
            _response_locator_map().pop(provider_id, None)
    record = load_control(provider_id, CONTROL_RESPONSE)
    fingerprint = record.get("fingerprint") if isinstance(record, dict) else None
    if isinstance(fingerprint, dict) and _host_matches(_page_host(page), str(record.get("host") or "")):
        for selector in selector_candidates(fingerprint, CONTROL_RESPONSE):
            try:
                response = visible_locator(page, selector)
            except Exception:
                continue
            if response is not None:
                _remember_source(provider_id, CONTROL_RESPONSE, "learned")
                return response
    return discover_response(page, provider_id)


def response_count(
    page: Any,
    provider_id: str,
    selectors: list[str] | tuple[str, ...],
) -> int:
    for selector in selectors:
        try:
            count = int(page.locator(selector).count())
        except Exception:
            continue
        if count:
            return count
    response = locate_response(page, provider_id, selectors)
    return 1 if response is not None else 0


def discover_response(page: Any, provider_id: str) -> Any | None:
    if not _provider_host_allowed(page, provider_id):
        return None
    watches = getattr(_context, "response_watches", {})
    token = watches.get(provider_id, "") if isinstance(watches, dict) else ""
    candidates = discovery.response_candidates(page, token)
    found = discovery.select_response_candidate(candidates)
    if found is None:
        return None
    return _remember_discovered_response(page, provider_id, found)


def request_doctor_response(page: Any, provider_id: str) -> Any | None:
    """Ask once only after normal response discovery has exhausted its wait."""
    if not _provider_host_allowed(page, provider_id):
        return None
    watches = getattr(_context, "response_watches", {})
    token = watches.get(provider_id, "") if isinstance(watches, dict) else ""
    found = _doctor_selection(
        page,
        provider_id,
        CONTROL_RESPONSE,
        discovery.response_candidates(page, token),
    )
    if found is None:
        return None
    return _remember_discovered_response(page, provider_id, found)


def teach_response(page: Any, provider_id: str) -> Any | None:
    if not can_teach():
        return None
    response = request_teaching(page, provider_id, CONTROL_RESPONSE)
    _response_locator_map()[provider_id] = response
    return response


def recover_response(
    page: Any,
    provider_id: str,
    read: Callable[[], str],
) -> str | None:
    """Validate Doctor first, then use human teaching only if it did not work."""
    for locate in (request_doctor_response, teach_response):
        if locate(page, provider_id) is None:
            continue
        try:
            return read()
        except cancellation.TaskCancelled:
            raise
        except Exception:
            reject_control(provider_id, CONTROL_RESPONSE)
    return None


def _remember_discovered_response(
    page: Any,
    provider_id: str,
    found: discovery.Discovery,
) -> Any:
    fingerprint = fingerprint_from_click(found.fingerprint)
    response = found.locator
    _response_locator_map()[provider_id] = response
    _remember_pending(provider_id, CONTROL_RESPONSE, page, fingerprint, response)
    _remember_source(provider_id, CONTROL_RESPONSE, "pending")
    return response


def score_response_candidate(candidate: dict[str, Any]) -> int:
    return discovery.score_response_candidate(candidate)


def control_has_text(control: Any, expected: str) -> bool:
    """Verify that a composer accepted text across textarea and contenteditable UIs."""
    try:
        value = control.input_value()
    except Exception:
        try:
            value = control.inner_text()
        except Exception:
            return False
    return str(value or "") == expected


def request_teaching(
    page: Any,
    provider_id: str,
    action: str,
    *,
    require_enabled: bool = False,
) -> Any:
    cancellation.check()
    if not _provider_host_allowed(page, provider_id):
        raise TimeoutError("Provider page host does not match the recovery target")
    if not can_teach():
        label = CONTROL_LABELS.get(action, "control")
        raise TimeoutError(f"Could not find the {label} in the model page")
    request = ControlTeachRequest(
        provider_id=provider_id,
        action=action,
        page=page,
        session_id=str(getattr(_context, "session_id", "") or ""),
        require_enabled=require_enabled,
    )
    return _handler(request)


def _doctor_selection(
    page: Any,
    provider_id: str,
    action: str,
    candidates: tuple[discovery.Discovery, ...],
    *,
    require_enabled: bool = False,
) -> discovery.Discovery | None:
    cancellation.check()
    if not candidates or not can_doctor():
        return None
    key = (str(getattr(_context, "session_id", "") or ""), provider_id, action)
    attempts = _doctor_attempts()
    if key in attempts:
        return None
    eligible = tuple(
        item
        for item in candidates
        if _discovery_is_usable(item, action, require_enabled)
    )
    if not eligible:
        return None
    attempts.add(key)
    request = profile_doctor.make_request(
        provider_id,
        action,
        page,
        eligible,
        session_id=key[0],
    )
    try:
        with suppress_assistance():
            selected = _doctor_handler(request) if _doctor_handler is not None else None
    except cancellation.TaskCancelled:
        raise
    except Exception:
        return None
    if not selected or not selected.startswith("c"):
        return None
    try:
        index = int(selected[1:]) - 1
    except ValueError:
        return None
    return eligible[index] if 0 <= index < len(eligible) else None


def _discovery_is_usable(
    item: discovery.Discovery,
    action: str,
    require_enabled: bool,
) -> bool:
    fingerprint = fingerprint_from_click(item.fingerprint)
    return control_fingerprint_is_valid(fingerprint, action) and _usable(
        item.locator,
        require_enabled,
    )


def _doctor_attempts() -> set[tuple[str, str, str]]:
    attempts = getattr(_context, "doctor_attempts", None)
    if attempts is None:
        attempts = set()
        _context.doctor_attempts = attempts
    return attempts


def _assistance_suppressed() -> bool:
    return bool(getattr(_context, "assistance_depth", 0))


@contextmanager
def suppress_assistance():
    depth = int(getattr(_context, "assistance_depth", 0))
    _context.assistance_depth = depth + 1
    try:
        yield
    finally:
        _context.assistance_depth = depth


def start_click_capture(page: Any) -> str:
    cancellation.check()
    token = "capture_" + str(int(time.time() * 1000))
    page.evaluate(_INSTALL_CAPTURE_JS, {"token": token})
    return token


def finish_click_capture(
    page: Any,
    token: str,
    action: str,
    *,
    timeout: float = 1.0,
) -> CapturedControl:
    deadline = time.time() + max(0.2, timeout)
    try:
        captured = _wait_for_click(page, deadline)
        fingerprint = fingerprint_from_click(captured)
        if control_fingerprint_is_valid(fingerprint, action):
            return CapturedControl(
                fingerprint=fingerprint,
                current_selector=f'[data-session-teach-current="{token}"]',
            )
        label = control_label(fingerprint)
        raise ValueError(label or "clicked item was not usable")
    finally:
        _cleanup_capture(page)


def cancel_click_capture(page: Any) -> None:
    _cleanup_capture(page)


def resolve_captured_control(request: ControlTeachRequest, captured: CapturedControl) -> Any:
    control = visible_locator(request.page, captured.current_selector)
    if _usable(control, request.require_enabled):
        _remember_pending(
            request.provider_id,
            request.action,
            request.page,
            captured.fingerprint,
            control,
        )
        _remember_source(request.provider_id, request.action, "pending")
        if request.action == CONTROL_RESPONSE:
            _response_locator_map()[request.provider_id] = control
        return control
    label = CONTROL_LABELS.get(request.action, "control")
    raise TimeoutError(f"Could not reuse the taught {label}")


def load_controls(path: Path | None = None) -> dict[str, Any]:
    path = path or CONTROL_STORE
    return read_json(
        path,
        max_bytes=provider_revival.MAX_PROVIDER_STORE_BYTES,
    ) or {}


def save_control(
    provider_id: str,
    action: str,
    page: Any,
    fingerprint: dict[str, Any],
    *,
    path: Path | None = None,
    verified: bool = False,
) -> None:
    path = path or CONTROL_STORE
    data = load_controls(path)
    provider = data.setdefault(provider_id, {})
    if not isinstance(provider, dict):
        provider = {}
        data[provider_id] = provider
    provider[action] = {
        "host": _page_host(page),
        "fingerprint": fingerprint,
        "verified": verified,
        "failures": 0,
    }
    try:
        write_json_atomic(
            path,
            data,
            max_bytes=provider_revival.MAX_PROVIDER_STORE_BYTES,
        )
    except (OSError, ValueError) as exc:
        raise OSError(f"Could not save the learned control: {path}") from exc


def load_control(provider_id: str, action: str, *, path: Path | None = None) -> dict[str, Any] | None:
    data = load_controls(path)
    provider = data.get(provider_id)
    if not isinstance(provider, dict):
        return None
    record = provider.get(action)
    return record if isinstance(record, dict) else None


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
        sources = {}
        _context.sources = sources
    return sources


def _pending_map() -> dict[tuple[str, str], _PendingControl]:
    pending = getattr(_context, "pending", None)
    if pending is None:
        pending = {}
        _context.pending = pending
    return pending


def _response_locator_map() -> dict[str, Any]:
    locators = getattr(_context, "response_locators", None)
    if locators is None:
        locators = {}
        _context.response_locators = locators
    return locators


def _revival_attempts() -> dict[str, _RevivalSend]:
    attempts = getattr(_context, "revival_attempts", None)
    if attempts is None:
        attempts = {}
        _context.revival_attempts = attempts
    return attempts


def _flow_cache() -> dict[tuple[str, str, str], dict[str, tuple[str, ...]] | None]:
    cache = getattr(_context, "flow_cache", None)
    if cache is None:
        cache = {}
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
    flow_recipe = _load_flow_cached(CONTROL_STORE, provider_id, profile_digest)
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
    except (OSError, ValueError):
        pass


def _clear_pending_controls(provider_id: str) -> None:
    for key in tuple(_pending_map()):
        if key[0] == provider_id:
            _pending_map().pop(key, None)


def _remember_pending(
    provider_id: str,
    action: str,
    page: Any,
    fingerprint: dict[str, Any],
    locator: Any | None = None,
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


def fingerprint_from_click(data: Any) -> dict[str, Any]:
    raw = data if isinstance(data, dict) else {}
    return {
        "tag": _clean(raw.get("tag"), 32).lower(),
        "role": _clean(raw.get("role"), 48).lower(),
        "type": _clean(raw.get("type"), 32).lower(),
        "aria_label": _clean(raw.get("ariaLabel"), 80),
        "title": _clean(raw.get("title"), 80),
        "placeholder": _clean(raw.get("placeholder"), 120),
        "text": _clean(raw.get("text"), 80),
        "contenteditable": bool(raw.get("contentEditable")),
        "classes": _stable_classes(raw.get("classes")),
        "data": _stable_data(raw.get("data")),
    }


def selector_candidates(fingerprint: dict[str, Any], action: str = "") -> list[str]:
    tag = _selector_tag(fingerprint.get("tag"))
    data = fingerprint.get("data") if isinstance(fingerprint.get("data"), dict) else {}
    candidates: list[str] = []
    for attr in ("data-testid", "data-test-id", "data-track-id", "data-track-name", "data-qa", "data-role"):
        value = _clean(data.get(attr), 120)
        if value:
            candidates.append(f'{tag}[{attr}={_css_str(value)}]')
            candidates.append(f'[{attr}={_css_str(value)}]')

    for attr in ("aria_label", "title", "placeholder"):
        value = _clean(fingerprint.get(attr), 120)
        if not value:
            continue
        html_attr = attr.replace("_", "-")
        candidates.append(f'{tag}[{html_attr}={_css_str(value)}]')
        candidates.append(f'[{html_attr}={_css_str(value)}]')

    input_type = _clean(fingerprint.get("type"), 32)
    if input_type:
        candidates.append(f'{tag}[type={_css_str(input_type)}]')

    classes = [cls for cls in fingerprint.get("classes", []) if _stable_token(cls)]
    if classes:
        parts = "".join(f'[class~={_css_str(cls)}]' for cls in classes[:3])
        candidates.append(f"{tag}{parts}")

    text = _clean(fingerprint.get("text"), 48)
    if text and action == CONTROL_SEND_BUTTON:
        candidates.append(f'{tag}:has-text({_css_str(text)})')
        candidates.append(f'[role="button"]:has-text({_css_str(text)})')
        candidates.append(f'button:has-text({_css_str(text)})')

    return _dedupe(candidates)


def control_fingerprint_is_valid(fingerprint: dict[str, Any], action: str) -> bool:
    if action == CONTROL_MESSAGE_BOX:
        return _looks_like_message_box(fingerprint)
    if action == CONTROL_SEND_BUTTON:
        return _looks_like_send_button(fingerprint) and not _looks_like_upload(fingerprint)
    if action == CONTROL_RESPONSE:
        return _looks_like_response(fingerprint)
    return True


def control_label(fingerprint: dict[str, Any]) -> str:
    for key in ("aria_label", "title", "placeholder", "text", "role", "tag"):
        value = _clean(fingerprint.get(key), 80)
        if value:
            return value
    return ""


def _wait_for_click(page: Any, deadline: float) -> dict[str, Any]:
    while time.time() < deadline:
        cancellation.check()
        captured = page.evaluate("window.__sessionTeachClick || null")
        if captured:
            return captured
        cancellation.wait(0.1)
    raise TimeoutError("Timed out waiting for a click in the model page")


def _cleanup_capture(page: Any) -> None:
    try:
        page.evaluate("window.__sessionTeachCleanup && window.__sessionTeachCleanup()")
    except Exception:
        pass


def _usable(control: Any | None, require_enabled: bool) -> bool:
    if control is None:
        return False
    if not require_enabled:
        return True
    try:
        return bool(control.is_enabled())
    except Exception:
        return False


def _page_host(page: Any) -> str:
    try:
        return urlparse(str(page.url or "")).netloc.lower()
    except Exception:
        return ""


def _host_matches(current: str, saved: str) -> bool:
    if not saved:
        return True
    return current == saved or current.endswith("." + saved) or saved.endswith("." + current)


def _provider_host_allowed(page: Any, provider_id: str) -> bool:
    current = _page_host(page)
    try:
        hosts = get_profile(provider_id).hosts
    except KeyError:
        return False
    return bool(current) and any(_host_matches(current, host) for host in hosts)


def _clean(value: Any, limit: int = 120) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def _stable_classes(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [cls for cls in (_clean(item, 64) for item in value) if _stable_token(cls)][:8]


def _stable_data(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, str] = {}
    for key, item in value.items():
        attr = _clean(key, 48)
        if not attr.startswith("data-") or attr.startswith("data-session-"):
            continue
        text = _clean(item, 120)
        if text:
            out[attr] = text
    return out


def _stable_token(value: str) -> bool:
    if not value or len(value) > 48:
        return False
    if re.fullmatch(r"[\da-f]{6,}", value, re.IGNORECASE):
        return False
    return bool(re.search(r"[a-zA-Z]", value))


def _selector_tag(value: Any) -> str:
    tag = _clean(value, 32).lower()
    return tag if re.fullmatch(r"[a-z][a-z0-9-]*", tag) else "*"


def _css_str(value: str) -> str:
    return json.dumps(value)


def _dedupe(items: list[str]) -> list[str]:
    out: list[str] = []
    for item in items:
        if item and item not in out:
            out.append(item)
    return out


def _combined_text(fingerprint: dict[str, Any]) -> str:
    data = fingerprint.get("data") if isinstance(fingerprint.get("data"), dict) else {}
    parts = [
        fingerprint.get("tag"),
        fingerprint.get("role"),
        fingerprint.get("type"),
        fingerprint.get("aria_label"),
        fingerprint.get("title"),
        fingerprint.get("placeholder"),
        fingerprint.get("text"),
        " ".join(fingerprint.get("classes", [])),
        " ".join(data.values()),
    ]
    return " ".join(_clean(part, 120).lower() for part in parts if part)


def _looks_like_message_box(fingerprint: dict[str, Any]) -> bool:
    tag = _clean(fingerprint.get("tag"), 32).lower()
    role = _clean(fingerprint.get("role"), 48).lower()
    input_type = _clean(fingerprint.get("type"), 32).lower()
    if tag == "input" and input_type in {"file", "hidden", "password"}:
        return False
    return tag in {"textarea", "input"} or role in {"textbox", "searchbox"} or bool(
        fingerprint.get("contenteditable") or fingerprint.get("placeholder")
    )


def _looks_like_response(fingerprint: dict[str, Any]) -> bool:
    tag = _clean(fingerprint.get("tag"), 32).lower()
    if tag in {"button", "textarea", "input", "nav", "header", "footer"}:
        return False
    return bool(
        fingerprint.get("text")
        or fingerprint.get("classes")
        or fingerprint.get("data")
    )


def _looks_like_send_button(fingerprint: dict[str, Any]) -> bool:
    tag = _clean(fingerprint.get("tag"), 32).lower()
    role = _clean(fingerprint.get("role"), 48).lower()
    if tag == "button" or role == "button":
        return True
    classes = {
        _clean(item, 64).lower()
        for item in fingerprint.get("classes", [])
        if isinstance(item, str)
    }
    if "enter" in classes:
        return True
    text = _combined_text(fingerprint)
    return any(word in text for word in ("send", "submit", "发送", "送出"))


def _looks_like_upload(fingerprint: dict[str, Any]) -> bool:
    text = _combined_text(fingerprint)
    return any(word in text for word in ("upload", "attach", "file", "上传", "附件"))


_INSTALL_CAPTURE_JS = r"""
({ token }) => {
  if (window.__sessionTeachCleanup) window.__sessionTeachCleanup();
  window.__sessionTeachClick = null;
  const interactive = 'button,[role="button"],textarea,input,[contenteditable="true"],[contenteditable="plaintext-only"]';
  const handler = (event) => {
    const raw = event.target;
    const el = raw && raw.closest ? (raw.closest(interactive) || raw) : raw;
    if (!el || !el.getBoundingClientRect) return;
    event.preventDefault();
    event.stopPropagation();
    event.stopImmediatePropagation();
    el.setAttribute('data-session-teach-current', token);
    const data = {};
    for (const attr of Array.from(el.attributes || [])) {
      if (attr.name && attr.name.startsWith('data-')) data[attr.name] = attr.value || '';
    }
    window.__sessionTeachClick = {
      tag: (el.tagName || '').toLowerCase(),
      role: el.getAttribute('role') || '',
      type: el.getAttribute('type') || '',
      ariaLabel: el.getAttribute('aria-label') || '',
      title: el.getAttribute('title') || '',
      placeholder: el.getAttribute('placeholder') || '',
      text: (el.innerText || el.textContent || '').trim(),
      contentEditable: !!el.isContentEditable,
      classes: Array.from(el.classList || []),
      data,
    };
    window.__sessionTeachCleanup();
  };
  document.addEventListener('click', handler, true);
  window.__sessionTeachCleanup = () => {
    document.removeEventListener('click', handler, true);
    window.__sessionTeachCleanup = null;
  };
}
"""
