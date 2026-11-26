"""Provider control location, discovery, teaching, and response recovery orchestration."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from playwright.sync_api import Locator
from codey.providers import discovery, profile_doctor
from codey.providers.assistance import assistance_suppressed, suppress_assistance
from codey.providers.control_capture import (
    CapturedControl,
    ControlTeachCancelled,
    ControlTeachRequest,
    cancel_click_capture,
    finish_click_capture,
    start_click_capture,
)
from codey.providers.control_fingerprint import (
    CONTROL_LABELS,
    CONTROL_MESSAGE_BOX,
    CONTROL_RESPONSE,
    CONTROL_SEND_BUTTON,
    _host_matches,
    _page_host,
    _provider_host_allowed,
    control_fingerprint_is_valid,
    control_label,
    fingerprint_from_click,
    selector_candidates,
)
from codey.providers.control_recovery import (
    _context,
    _doctor_attempts,
    _pending_map,
    _remember_pending,
    _remember_source,
    _response_locator_map,
    _source_for,
    begin_task_context,
    confirm_control,
    end_task_context,
    flow_matches,
    flow_stage_ready,
    logger,
    read_flow_response,
    recover_flow,
    reject_control,
    revival_send,
)
from codey.providers.control_store import CONTROL_STORE, load_control, load_controls, save_control
from codey.runtime.core import cancellation

__all__ = [
    "CONTROL_LABELS",
    "CONTROL_MESSAGE_BOX",
    "CONTROL_RESPONSE",
    "CONTROL_SEND_BUTTON",
    "CONTROL_STORE",
    "CapturedControl",
    "ControlTeachCancelled",
    "ControlTeachRequest",
    "begin_task_context",
    "can_doctor",
    "can_teach",
    "cancel_click_capture",
    "confirm_control",
    "control_fingerprint_is_valid",
    "control_has_text",
    "control_label",
    "discover_control",
    "discover_response",
    "end_task_context",
    "fingerprint_from_click",
    "finish_click_capture",
    "flow_matches",
    "flow_stage_ready",
    "load_control",
    "load_controls",
    "locate_control",
    "locate_response",
    "logger",
    "pending_control",
    "read_flow_response",
    "recover_flow",
    "recover_response",
    "reject_control",
    "request_doctor_response",
    "request_teaching",
    "resolve_captured_control",
    "response_count",
    "revival_send",
    "save_control",
    "saved_control",
    "score_control_candidate",
    "score_response_candidate",
    "selector_candidates",
    "set_doctor_handler",
    "set_teach_handler",
    "start_click_capture",
    "start_response_watch",
    "stop_response_watch",
    "suppress_assistance",
    "teach_response",
    "unique_visible_locator",
    "visible_locator",
]
_handler: Callable[[ControlTeachRequest], Locator] | None = None
_doctor_handler: Callable[[profile_doctor.ProfileDoctorRequest], str | None] | None = None


def set_teach_handler(handler: Callable[[ControlTeachRequest], Locator] | None) -> None:
    global _handler
    _handler = handler


def set_doctor_handler(
    handler: Callable[[profile_doctor.ProfileDoctorRequest], str | None] | None,
) -> None:
    global _doctor_handler
    _doctor_handler = handler


def can_teach() -> bool:
    return _handler is not None and not assistance_suppressed()


def can_doctor() -> bool:
    return _doctor_handler is not None and not assistance_suppressed()


def visible_locator(page: Any, selector: str) -> Locator | None:
    locator: Locator = page.locator(selector)
    try:
        count = locator.count()
    except cancellation.TaskCancelled:
        raise
    except Exception:
        return None
    try:
        count = count
    except (TypeError, ValueError, OverflowError):
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
) -> Locator | None:
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
) -> Locator | None:
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
) -> Locator | None:
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


def unique_visible_locator(page: Any, selector: str) -> Locator | None:
    """Return a visible match only when the learned selector is unambiguous."""
    locator: Locator = page.locator(selector)
    matches: list[Locator] = []
    try:
        count = locator.count()
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
) -> Locator | None:
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
        watches = dict[str, str]()
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
) -> Locator | None:
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
    if not isinstance(record, dict):
        return discover_response(page, provider_id)
    fingerprint = record.get("fingerprint")
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


def discover_response(page: Any, provider_id: str) -> Locator | None:
    if not _provider_host_allowed(page, provider_id):
        return None
    watches = getattr(_context, "response_watches", {})
    token = watches.get(provider_id, "") if isinstance(watches, dict) else ""
    candidates = discovery.response_candidates(page, token)
    found = discovery.select_response_candidate(candidates)
    if found is None:
        return None
    return _remember_discovered_response(page, provider_id, found)


def request_doctor_response(page: Any, provider_id: str) -> Locator | None:
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


def teach_response(page: Any, provider_id: str) -> Locator | None:
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
) -> Locator:
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
) -> Locator:
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
    handler = _handler
    if handler is None:
        raise TimeoutError("control teaching is unavailable")
    return handler(request)


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


def _usable(control: Any | None, require_enabled: bool) -> bool:
    if control is None:
        return False
    if not require_enabled:
        return True
    try:
        return bool(control.is_enabled())
    except Exception:
        return False
