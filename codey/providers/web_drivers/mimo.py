"""Xiaomi MiMo Chat web driver using selectors verified against the live page."""

from __future__ import annotations

import json
import time

from playwright.sync_api import Locator, Page

from codey.runtime import cancellation
from codey.providers import controls as controls
from codey.providers import flow as provider_flow
from codey.providers import send_loop as send_loop
from codey.providers.profiles import get_profile
from codey.providers.diagnostics import ControlMissing, ResponseMissing
from codey.providers.timeouts import navigation_timeout_ms, remaining, start_deadline
from codey.providers.web_drivers import common as driver_common
from codey.providers.submission import (
    SendAttempt,
    SubmissionUncertain,
    confirm_submission,
)
from codey.toolchain.json_reply import (
    is_json_tool_reply as _is_json_tool_reply,
    normalize_final_json_tool_reply as _normalize_final_json_tool_reply,
)
from codey.automation.web_clipboard import copy_action_text

PROVIDER_ID = "mimo"
PROFILE = get_profile(PROVIDER_ID)
MIMO_URL = "https://aistudio.xiaomimimo.com/#/c"
MIMO_ORIGIN = "https://aistudio.xiaomimimo.com"
COPY_BUTTON = 'button[data-track-id="msg_copy_btn"][data-track-name="msg_copy"]'

READY_TIMEOUT = 90.0
TIMEOUT_GRACE = 60.0
COPY_READY_TIMEOUT = 10.0
SUBMIT_CONFIRM_TIMEOUT = 15.0
SUBMIT_READY_TIMEOUT = 5.0
RESPONSE_FOOTER_TIMEOUT = 4.0
RESPONSE_FOOTER_STABLE_TICKS = 2

_RESPONSE_TEXT_JS = r"""
el => {
  const clone = el.cloneNode(true);
  for (const hidden of Array.from(clone.querySelectorAll('[aria-hidden="true"], [hidden]'))) {
    hidden.remove();
  }
  const thinking = /^(正在思考|已深度思考|深度思考|思考中)/;
  for (const summary of Array.from(clone.querySelectorAll('summary'))) {
    const text = (summary.innerText || summary.textContent || '').trim();
    if (!thinking.test(text)) continue;
    const block =
      summary.closest('.mb-2') ||
      summary.closest('[data-state]') ||
      summary.parentElement;
    if (block) block.remove();
  }
  return (clone.innerText || clone.textContent || '').trim();
}
"""


def _visible_locator(page: Page, selector: str) -> Locator | None:
    return controls.visible_locator(page, selector)


def _message_box(page: Page, *, teach: bool = False) -> Locator | None:
    return driver_common.message_box(PROVIDER_ID, PROFILE, page, teach=teach)


def _dismiss_known_notice(page: Page) -> bool:
    """Close only explicitly profiled, non-transactional announcements."""
    for selector in PROFILE.selectors("dismiss_notice"):
        button = _visible_locator(page, selector)
        if button is None:
            continue
        cancellation.check()
        try:
            button.click(timeout=2000)
        except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
            raise
        except Exception:
            try:
                button.evaluate("el => el.click()")
            except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
                raise
            except Exception:
                return False
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if _visible_locator(page, selector) is None:
                return True
            cancellation.wait(0.1)
        return False
    return False


def wait_ready(page: Page, timeout: float = READY_TIMEOUT) -> None:
    cancellation.check()
    deadline = time.time() + timeout
    while time.time() < deadline:
        _dismiss_known_notice(page)
        if _message_box(page) is not None and not _generation_active(page):
            return
        cancellation.wait(0.4)
    if _message_box(page, teach=True) is not None and not _generation_active(page):
        return
    raise TimeoutError("Xiaomi MiMo Chat input did not appear. Are you logged in?")


def new_chat(page: Page, timeout: float | None = None) -> None:
    cancellation.check()
    deadline = start_deadline(timeout)
    page.goto(
        MIMO_URL,
        wait_until="domcontentloaded",
        timeout=navigation_timeout_ms(deadline),
    )
    if deadline is None:
        wait_ready(page)
    else:
        wait_ready(page, timeout=remaining(deadline, READY_TIMEOUT))


def _response_count(page: Page) -> int:
    return driver_common.response_count(PROVIDER_ID, PROFILE, page)


def _last_text(page: Page) -> str:
    response = controls.locate_response(page, PROVIDER_ID, PROFILE.selectors("response"))
    if response is None:
        return ""
    return _response_text(response)


def _response_text(response: Locator) -> str:
    try:
        return str(response.evaluate(_RESPONSE_TEXT_JS) or "").strip()
    except Exception:
        pass
    try:
        text = str(response.inner_text() or "").strip()
    except Exception:
        return ""
    if _starts_with_thinking_summary(text):
        return ""
    return text


def _starts_with_thinking_summary(text: str) -> bool:
    stripped = str(text or "").lstrip()
    return stripped.startswith(("正在思考", "已深度思考", "深度思考", "思考中"))


def _generation_complete(page: Page) -> bool:
    response = controls.locate_response(page, PROVIDER_ID, PROFILE.selectors("response"))
    if response is None:
        return False
    return _response_complete(page, response)


def _response_complete(page: Page, response: Locator) -> bool:
    if not _response_text(response):
        return False
    if _response_is_typing(response):
        return False
    if _copy_button_after_response(page, response) is not None:
        return True
    return not _generation_active(page)


def _response_is_typing(response: Locator) -> bool:
    return _response_typing_state(response) is True


def _response_typing_state(response: Locator) -> bool | None:
    try:
        value = response.evaluate("el => el.getAttribute('data-is-typing')")
    except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
        raise
    except Exception:
        return None
    normalized = str(value).strip().lower() if value is not None else ""
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    return None


def _completion_observation(
    response: Locator | None,
    *,
    current: str,
    stable: bool,
) -> provider_flow.FlowObservation:
    typing_state = _response_typing_state(response) if response is not None else None
    return provider_flow.FlowObservation(
        response_stable=stable,
        response_nonempty=bool(current),
        typing_true=typing_state is True,
        typing_false=typing_state is False,
    )


def _copy_button_after_response(page: Page, response: Locator) -> Locator | None:
    try:
        response_box = response.bounding_box()
    except Exception:
        response_box = None
    if not isinstance(response_box, dict):
        return None
    x_min = max(0.0, float(response_box["x"]) - 8.0)
    y_min = float(response_box["y"] + response_box["height"]) - 8.0
    y_max = float(response_box["y"] + response_box["height"]) + 120.0
    buttons = page.locator(COPY_BUTTON)
    matches: list[tuple[float, float, Locator]] = []
    for index in range(buttons.count()):
        button = buttons.nth(index)
        try:
            if not button.is_visible():
                continue
            box = button.bounding_box()
        except Exception:
            continue
        if not box:
            continue
        if box["x"] < x_min or box["y"] < y_min or box["y"] > y_max:
            continue
        matches.append((float(box["x"]), float(box["y"]), button))
    if not matches:
        return None
    matches.sort(key=lambda item: (item[1], item[0]))
    return matches[0][2]


def _response_action_count(page: Page) -> int:
    count = 0
    try:
        buttons = page.locator(COPY_BUTTON)
        total = int(buttons.count())
    except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
        raise
    except Exception:
        return 0
    for index in range(total):
        try:
            if buttons.nth(index).is_visible():
                count += 1
        except Exception:
            continue
    return count


def _wait_response_footer_ready(
    page: Page,
    action_baseline: int,
    timeout: float = RESPONSE_FOOTER_TIMEOUT,
) -> None:
    deadline = time.time() + max(0.0, timeout)
    stable = 0
    last = -1
    while time.time() < deadline:
        current = _response_action_count(page)
        if current > action_baseline and current == last:
            stable += 1
            if stable >= RESPONSE_FOOTER_STABLE_TICKS:
                return
        else:
            stable = 0
            last = current
        cancellation.wait(0.25)


def _copy_last_text(page: Page) -> str:
    """Use MiMo's copy action to recover raw text before Markdown rendering."""
    if not _generation_complete(page):
        return ""
    response = controls.locate_response(page, PROVIDER_ID, PROFILE.selectors("response"))
    if response is None:
        return ""
    visible_text = _response_text(response)
    if not visible_text:
        return ""
    deadline = time.time() + COPY_READY_TIMEOUT
    while time.time() < deadline:
        try:
            response.scroll_into_view_if_needed(timeout=1000)
        except Exception:
            pass
        action = _copy_button_after_response(page, response)
        if action is not None:
            cancellation.check()
            raw = copy_action_text(page, action, origin=MIMO_ORIGIN)
            return _clean_copied_text(raw, visible_text)
        cancellation.wait(0.2)
    return ""


def _clean_copied_text(raw: str, visible_text: str) -> str:
    text = str(raw or "").strip()
    visible = str(visible_text or "").strip()
    if not text:
        return ""
    if not visible:
        return "" if _starts_with_thinking_summary(text) else text
    if text == visible:
        return text
    if _starts_with_thinking_summary(text):
        return visible
    return text


def _normalize_mimo_json_tool_reply(text: str) -> str:
    clean = str(text or "").strip()
    if not clean:
        return ""
    repaired = _normalize_final_json_tool_reply(clean)
    if _is_json_tool_reply(repaired):
        return repaired

    fenced = _markdown_json_payload(clean)
    if fenced:
        repaired = _normalize_final_json_tool_reply(fenced)
        if _is_json_tool_reply(repaired):
            return repaired
        deduped = _dedupe_identical_json_tool_objects(fenced)
        if deduped:
            return deduped

    deduped = _dedupe_identical_json_tool_objects(clean)
    return deduped or clean


def _markdown_json_payload(text: str) -> str:
    lines = str(text or "").strip().splitlines()
    if len(lines) < 3:
        return ""
    opener = lines[0].strip().casefold()
    if opener not in {"```", "```json"} or lines[-1].strip() != "```":
        return ""
    return "\n".join(lines[1:-1]).strip()


def _dedupe_identical_json_tool_objects(text: str) -> str:
    clean = _without_mimo_json_markers(text)
    if not clean:
        return ""
    objects = _parse_only_json_objects(clean)
    if not 1 <= len(objects) <= 2:
        return ""
    first = objects[0]
    if not isinstance(first, dict) or any(obj != first for obj in objects):
        return ""
    candidate = json.dumps(first, ensure_ascii=False, separators=(",", ":"))
    return candidate if _is_json_tool_reply(candidate) else ""


def _without_mimo_json_markers(text: str) -> str:
    lines = str(text or "").strip().splitlines()
    kept: list[str] = []
    removed = False
    for line in lines:
        if line.strip().casefold() == "json":
            removed = True
            continue
        kept.append(line)
    return "\n".join(kept).strip() if removed else str(text or "").strip()


def _parse_only_json_objects(text: str) -> list[object]:
    clean = str(text or "")
    decoder = json.JSONDecoder()
    values: list[object] = []
    index = 0
    while index < len(clean):
        while index < len(clean) and clean[index].isspace():
            index += 1
        if index >= len(clean):
            break
        try:
            value, end = decoder.raw_decode(clean, index)
        except ValueError:
            return []
        values.append(value)
        index = end
    return values


def _final_text(page: Page, *, completion_verified: bool = False) -> str:
    raw = "" if completion_verified else _copy_last_text(page)
    if raw:
        raw = _normalize_mimo_json_tool_reply(raw)
        controls.confirm_control(PROVIDER_ID, controls.CONTROL_RESPONSE)
        return raw
    if not completion_verified and not _generation_complete(page):
        raise RuntimeError("Xiaomi MiMo response is still generating")
    fallback = _last_text(page)
    if fallback:
        fallback = _normalize_mimo_json_tool_reply(fallback)
        controls.confirm_control(PROVIDER_ID, controls.CONTROL_RESPONSE)
        return fallback
    controls.reject_control(PROVIDER_ID, controls.CONTROL_RESPONSE)
    raise RuntimeError("Could not read the raw Xiaomi MiMo response")


def _submission_started(
    page: Page,
    baseline: int,
    baseline_text: str = "",
    submitted_text: str = "",
) -> bool:
    try:
        count = _response_count(page)
        current = _last_text(page) if count else ""
    except Exception:
        return False
    if count > baseline or (bool(current) and current != baseline_text):
        return True
    if submitted_text:
        textarea = _message_box(page)
        try:
            input_empty = textarea is not None and not textarea.input_value().strip()
            if input_empty:
                return True
            return controls.flow_matches(
                PROVIDER_ID,
                provider_flow.STAGE_SUBMISSION,
                provider_flow.FlowObservation(
                    input_empty=input_empty,
                    response_count_increased=count > baseline,
                ),
            )
        except Exception:
            return False
    return False


def _submit(
    page: Page,
    baseline: int,
    baseline_text: str = "",
    submitted_text: str = "",
) -> SendAttempt:
    textarea = _message_box(page, teach=True)
    if textarea is None:
        controls.reject_control(
            PROVIDER_ID, controls.CONTROL_MESSAGE_BOX, page=page
        )
        raise ControlMissing(
            "Xiaomi MiMo Chat input is not visible",
            stage=provider_flow.STAGE_SUBMISSION,
        )

    button = _send_button(page, teach=True)
    attempt = SendAttempt()
    if button is not None:
        attempt.submit("click", button.click)
    else:
        controls.reject_control(
            PROVIDER_ID, controls.CONTROL_SEND_BUTTON, page=page
        )
        if _generation_active(page):
            raise TimeoutError("Xiaomi MiMo Chat is still generating; refusing to submit")
        attempt.submit("enter", lambda: textarea.press("Enter"))
    if _wait_submission_started(page, baseline, baseline_text, submitted_text):
        confirm_submission(attempt, PROVIDER_ID)
    return attempt


def _send_button(page: Page, *, teach: bool = False) -> Locator | None:
    deadline = time.time() + SUBMIT_READY_TIMEOUT
    first = True
    while first or time.time() < deadline:
        first = False
        control = _profiled_send_button(page)
        if control is not None:
            return control
        if time.time() >= deadline:
            break
        cancellation.wait(0.2)
    if teach:
        return controls.locate_control(
            page,
            PROVIDER_ID,
            controls.CONTROL_SEND_BUTTON,
            (),
            require_enabled=True,
            teach=True,
            anchor=_message_box(page),
        )
    return None


def _profiled_send_button(page: Page) -> Locator | None:
    """Return only MiMo's explicit send button, never nearby upload controls."""
    for selector in PROFILE.selectors("send_button"):
        try:
            candidates = page.locator(selector)
            count = int(candidates.count())
        except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
            raise
        except Exception:
            continue
        for index in range(count - 1, -1, -1):
            candidate = candidates.nth(index)
            if _is_mimo_send_button(candidate):
                return candidate
    return None


def _is_mimo_send_button(candidate: Locator) -> bool:
    state = _mimo_button_state(candidate)
    return _is_mimo_enabled_send_state(state)


def _mimo_button_state(candidate: Locator) -> dict[str, object] | None:
    try:
        if not candidate.is_visible():
            return None
        enabled = bool(candidate.is_enabled())
        state = candidate.evaluate(
            """el => ({
                disabled: !!el.disabled,
                ariaDisabled: el.getAttribute('aria-disabled') || '',
                trackId: el.getAttribute('data-track-id') || '',
                trackName: el.getAttribute('data-track-name') || '',
                text: el.innerText || el.textContent || '',
                aria: el.getAttribute('aria-label') || '',
                title: el.getAttribute('title') || '',
                viewBoxes: Array.from(el.querySelectorAll('svg')).map(svg => svg.getAttribute('viewBox') || ''),
                rect: (() => {
                    const rect = el.getBoundingClientRect();
                    return {
                        left: rect.left,
                        right: rect.right,
                        top: rect.top,
                        bottom: rect.bottom,
                        width: rect.width,
                        height: rect.height,
                        viewportWidth: window.innerWidth,
                        viewportHeight: window.innerHeight
                    };
                })()
            })"""
        )
    except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
        raise
    except Exception:
        return None
    if not isinstance(state, dict):
        return None
    state["enabled"] = enabled
    return state


def _is_mimo_enabled_send_state(state: dict[str, object] | None) -> bool:
    if not _is_mimo_idle_send_state(state):
        return False
    aria_disabled = str((state or {}).get("ariaDisabled") or "").lower()
    return (
        bool((state or {}).get("enabled"))
        and not (state or {}).get("disabled")
        and aria_disabled != "true"
    )


def _is_mimo_idle_send_state(state: dict[str, object] | None) -> bool:
    if not state:
        return False
    track_id = str((state or {}).get("trackId") or "")
    track_name = str((state or {}).get("trackName") or "")
    text = " ".join(
        str((state or {}).get(name) or "").lower()
        for name in ("text", "aria", "title")
    )
    return (
        track_id == "home_send_btn"
        and track_name == "home_send_message"
        and _button_in_viewport(state)
        and _has_mimo_send_icon(state)
        and not any(word in text for word in ("stop", "停止", "终止"))
    )


def _generation_active(page: Page) -> bool:
    for selector in PROFILE.selectors("send_button"):
        try:
            candidates = page.locator(selector)
            count = int(candidates.count())
        except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
            raise
        except Exception:
            continue
        for index in range(count - 1, -1, -1):
            state = _mimo_button_state(candidates.nth(index))
            if _is_mimo_active_stop_state(state):
                return True
    return False


def _is_mimo_active_stop_state(state: dict[str, object] | None) -> bool:
    if not state:
        return False
    track_id = str(state.get("trackId") or "")
    track_name = str(state.get("trackName") or "")
    aria_disabled = str(state.get("ariaDisabled") or "").lower()
    return (
        track_id == "home_send_btn"
        and track_name == "home_send_message"
        and bool(state.get("enabled"))
        and not state.get("disabled")
        and aria_disabled != "true"
        and _button_in_viewport(state)
        and not _has_mimo_send_icon(state)
    )


def _button_in_viewport(state: dict[str, object]) -> bool:
    rect = state.get("rect")
    if not isinstance(rect, dict):
        return False
    try:
        width = float(rect.get("width") or 0)
        height = float(rect.get("height") or 0)
        left = float(rect.get("left") or 0)
        right = float(rect.get("right") or 0)
        top = float(rect.get("top") or 0)
        bottom = float(rect.get("bottom") or 0)
        viewport_width = float(rect.get("viewportWidth") or 0)
        viewport_height = float(rect.get("viewportHeight") or 0)
    except (TypeError, ValueError):
        return False
    return (
        width > 0
        and height > 0
        and right > 0
        and bottom > 0
        and left < viewport_width
        and top < viewport_height
    )


def _has_mimo_send_icon(state: dict[str, object]) -> bool:
    view_boxes = state.get("viewBoxes")
    if not isinstance(view_boxes, list):
        return False
    return any(str(value).strip() == "0 0 19 16" for value in view_boxes)


def _wait_submission_started(
    page: Page,
    baseline: int,
    baseline_text: str = "",
    submitted_text: str = "",
    timeout: float = SUBMIT_CONFIRM_TIMEOUT,
) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _submission_started(page, baseline, baseline_text, submitted_text):
            return True
        cancellation.wait(0.2)
    return False


def _wait_late_response(
    page: Page,
    baseline: int,
    baseline_text: str = "",
    grace: float = TIMEOUT_GRACE,
    tick: float = 0.8,
) -> str:
    last = ""

    def _ready() -> str:
        nonlocal last
        count = _response_count(page)
        current = _last_text(page) if count else ""
        if (
            current
            and (count > baseline or current != baseline_text)
            and _generation_complete(page)
        ):
            last = current
            return _final_text(page)
        return ""

    return driver_common.poll_late_response(
        _ready,
        grace=grace,
        tick=tick,
        default=lambda: last,
    )


def _chat(
    page: Page,
    text: str,
    response_timeout: float = 300.0,
    stable_ticks: int = 4,
    tick: float = 0.8,
    min_wait: float = 1.5,
) -> str:
    """Send one message and return the final raw answer text from MiMo Chat."""
    cancellation.check()
    wait_ready(page)

    baseline = _response_count(page)
    baseline_text = _last_text(page) if baseline else ""
    action_baseline = _response_action_count(page)

    with send_loop.response_watch(page, PROVIDER_ID):
        textarea = _message_box(page, teach=True)
        if textarea is None:
            controls.reject_control(
                PROVIDER_ID, controls.CONTROL_MESSAGE_BOX, page=page
            )
            raise ControlMissing("Xiaomi MiMo Chat input is not visible")
        try:
            cancellation.check()
            textarea.click()
            cancellation.check()
            textarea.fill(text)
        except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
            raise
        except Exception:
            controls.reject_control(PROVIDER_ID, controls.CONTROL_MESSAGE_BOX)
            raise
        if not controls.control_has_text(textarea, text):
            controls.reject_control(PROVIDER_ID, controls.CONTROL_MESSAGE_BOX)
            raise ControlMissing("Xiaomi MiMo Chat input did not accept the complete message")
        controls.confirm_control(PROVIDER_ID, controls.CONTROL_MESSAGE_BOX)
        cancellation.wait(0.2)
        attempt = _submit(page, baseline, baseline_text, text)

        ctx = send_loop.ProviderSendContext(
            page=page,
            provider_id=PROVIDER_ID,
            display_name="Xiaomi MiMo",
            sent_at=time.time(),
        )
        deadline = ctx.sent_at + response_timeout
        answer_text_seen = False
        while time.time() < deadline:
            cancellation.wait(min(tick, 0.2) if not answer_text_seen else tick)
            count = _response_count(page)
            response = (
                controls.locate_response(page, PROVIDER_ID, PROFILE.selectors("response"))
                if count
                else None
            )
            current = _response_text(response) if response is not None else ""
            if count <= baseline and current == baseline_text:
                continue
            confirm_submission(attempt, PROVIDER_ID)
            same = ctx.same_as_last(current)
            observation = _completion_observation(
                response,
                current=current,
                stable=same,
            )
            ctx.record_response(current, observation)
            if not current:
                continue
            answer_text_seen = True
            if ctx.stable >= stable_ticks and (time.time() - ctx.sent_at) >= min_wait:
                built_in_ready = _generation_complete(page)
                completion_ready = send_loop.completion_ready(
                    ctx,
                    observation,
                    built_in_ready=built_in_ready,
                    allow_recovery=attempt.confirmed,
                )
                if completion_ready:
                    _wait_response_footer_ready(page, action_baseline)
                    return send_loop.read_completion(
                        ctx,
                        lambda: _final_text(
                            page,
                            completion_verified=not built_in_ready,
                        ),
                    )

        late = _wait_late_response(
            page,
            baseline,
            baseline_text=baseline_text,
            grace=TIMEOUT_GRACE,
            tick=tick,
        )
        if late:
            confirm_submission(attempt, PROVIDER_ID)
            _wait_response_footer_ready(page, action_baseline)
            return late
        if ctx.appeared and ctx.last:
            response = controls.locate_response(
                page,
                PROVIDER_ID,
                PROFILE.selectors("response"),
            )
            observation = _completion_observation(
                response,
                current=ctx.last,
                stable=True,
            )
            ctx.trace.add(observation)
            built_in_ready = _generation_complete(page)
            completion_ready = send_loop.completion_ready(
                ctx,
                observation,
                built_in_ready=built_in_ready,
                allow_recovery=attempt.confirmed,
            )
            if completion_ready:
                _wait_response_footer_ready(page, action_baseline)
                return send_loop.read_completion(
                    ctx,
                    lambda: _final_text(
                        page,
                        completion_verified=not built_in_ready,
                    ),
                )
        recovered = controls.recover_response(page, PROVIDER_ID, lambda: _final_text(page))
        if recovered is not None:
            confirm_submission(attempt, PROVIDER_ID)
            _wait_response_footer_ready(page, action_baseline)
            return recovered
        if not attempt.confirmed:
            if attempt.method == "click" and attempt.action_error is not None:
                controls.reject_control(PROVIDER_ID, controls.CONTROL_SEND_BUTTON)
            raise SubmissionUncertain("Xiaomi MiMo Chat submission status is uncertain")
        raise ResponseMissing(f"Xiaomi MiMo response timed out after {response_timeout:.0f}s")


@controls.revival_send(PROVIDER_ID)
def chat(
    page: Page,
    text: str,
    response_timeout: float = 300.0,
    stable_ticks: int = 4,
    tick: float = 0.8,
    min_wait: float = 1.5,
) -> str:
    return _chat(page, text, response_timeout, stable_ticks, tick, min_wait)
