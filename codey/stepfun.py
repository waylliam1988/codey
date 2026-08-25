"""StepFun Chat web driver using selectors verified against the live page."""

from __future__ import annotations

import time

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page

from codey import (
    cancellation,
    provider_controls as controls,
    provider_flow,
    provider_send_loop as send_loop,
)
from codey.json_tool_reply import (
    is_json_tool_reply as _is_json_tool_reply,
    looks_like_json_tool_reply as _looks_like_json_tool_reply,
    normalize_final_json_tool_reply as _normalize_final_json_tool_reply,
    repair_missing_trailing_braces_json_tool_reply as _repair_missing_trailing_braces_json_tool_reply,
)
from codey.provider_diagnostics import ControlMissing, ResponseMissing
from codey.provider_profiles import get_profile
from codey.provider_submission import SendAttempt, SubmissionUncertain, confirm_submission
from codey.provider_timeouts import navigation_timeout_ms, remaining, start_deadline

PROVIDER_ID = "stepfun"
PROFILE = get_profile(PROVIDER_ID)
STEPFUN_URL = "https://chat.stepfun.com/chats/"

READY_TIMEOUT = 90.0
TIMEOUT_GRACE = 60.0
COMPOSER_SETTLE_TIME = 1.5
COMPOSER_SETTLE_TICK = 0.25
COMPOSER_REFILL_ATTEMPTS = 3
COMPOSER_REFILL_DELAY = 0.4
SUBMIT_CONFIRM_TIMEOUT = 15.0
JSON_TOOL_STABLE_TICKS = 2
RESPONSE_FOOTER_TIMEOUT = 8.0
RESPONSE_FOOTER_STABLE_TICKS = 2

_VISIBLE_RESPONSE_COUNT_JS = r"""
(selector) => Array.from(document.querySelectorAll(selector))
  .filter((el) => {
    if (el.closest('.reason-render-ext')) return false;
    const rect = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return rect.width > 0
      && rect.height > 0
      && style.visibility !== 'hidden'
      && style.display !== 'none';
  }).length
"""

_FRESH_RESPONSE_TEXT_JS = r"""
({selector, baseline}) => {
  const visible = (el) => {
    if (el.closest('.reason-render-ext')) return false;
    const rect = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return rect.width > 0
      && rect.height > 0
      && style.visibility !== 'hidden'
      && style.display !== 'none';
  };
  const textOf = (el) => String(el.innerText || el.textContent || '').trim();
  const els = Array.from(document.querySelectorAll(selector)).filter(visible);
  const freshCount = Math.max(0, els.length - Number(baseline || 0));
  return els.slice(0, freshCount).map(textOf).filter(Boolean).join('\n').trim();
}
"""

_LATEST_RESPONSE_TEXT_JS = r"""
(selector) => {
  const visible = (el) => {
    if (el.closest('.reason-render-ext')) return false;
    const rect = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return rect.width > 0
      && rect.height > 0
      && style.visibility !== 'hidden'
      && style.display !== 'none';
  };
  const els = Array.from(document.querySelectorAll(selector)).filter(visible);
  const first = els[0];
  return first ? String(first.innerText || first.textContent || '').trim() : '';
}
"""

_SET_TEXTAREA_VALUE_JS = r"""
(el, value) => {
  const setter = Object.getOwnPropertyDescriptor(
    window.HTMLTextAreaElement.prototype,
    'value'
  ).set;
  setter.call(el, value);
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
}
"""
def _message_box(page: Page, *, teach: bool = False) -> Locator | None:
    return controls.locate_control(
        page,
        PROVIDER_ID,
        controls.CONTROL_MESSAGE_BOX,
        PROFILE.selectors("message_box"),
        teach=teach,
    )


def _send_button(
    page: Page,
    *,
    timeout: float = 0.0,
    teach: bool = False,
) -> Locator | None:
    deadline = time.time() + max(0.0, timeout)
    first = True
    while first or time.time() < deadline:
        first = False
        for selector in PROFILE.selectors("send_button"):
            control = controls.visible_locator(page, selector)
            if control is None:
                continue
            try:
                if control.is_enabled():
                    return control
            except Exception:
                continue
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


def wait_ready(page: Page, timeout: float = READY_TIMEOUT) -> None:
    cancellation.check()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _message_box(page) is not None:
            return
        cancellation.wait(0.4)
    if _message_box(page, teach=True) is not None:
        return
    raise TimeoutError("StepFun Chat input did not appear. Are you logged in?")


def new_chat(page: Page, timeout: float | None = None) -> None:
    cancellation.check()
    deadline = start_deadline(timeout)
    try:
        page.goto(
            STEPFUN_URL,
            wait_until="domcontentloaded",
            timeout=navigation_timeout_ms(deadline),
        )
    except PlaywrightError as exc:
        if "net::ERR_ABORTED" not in str(exc):
            raise
    if deadline is None:
        wait_ready(page)
    else:
        wait_ready(page, timeout=remaining(deadline, READY_TIMEOUT))


def _response_selector() -> str:
    return PROFILE.selector("response")


def _response_count(page: Page) -> int:
    try:
        return int(page.evaluate(_VISIBLE_RESPONSE_COUNT_JS, _response_selector()))
    except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
        raise
    except Exception:
        return _response_count_fallback(page)


def _visible_responses_newest_first(page: Page) -> list[Locator]:
    """Visible response nodes in StepFun's own DOM order: newest first.

    A live DOM probe confirmed the newest reply sits at filtered index 0
    (head-first), so this fallback must NOT defer to the generic
    ``provider_controls.locate_response()`` scan, which walks from the tail
    and assumes oldest-first ordering -- it would read a stale reply.
    ``>> visible=true`` keeps duplicate/hidden nodes out of the count.
    """
    try:
        return list(
            page.locator(f"{_response_selector()} >> visible=true").all()
        )
    except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
        raise
    except Exception:
        return []


def _node_text(node: Locator) -> str:
    try:
        return str(node.inner_text() or "").strip()
    except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
        raise
    except Exception:
        return ""


def _response_count_fallback(page: Page) -> int:
    return len(_visible_responses_newest_first(page))


def _fresh_response_text_fallback(page: Page, baseline: int) -> str:
    els = _visible_responses_newest_first(page)
    fresh = els[: max(0, len(els) - max(0, int(baseline)))]
    return "\n".join(
        text for text in (_node_text(node) for node in fresh) if text
    ).strip()


def _latest_response_text_fallback(page: Page) -> str:
    els = _visible_responses_newest_first(page)
    return _node_text(els[0]) if els else ""


def _response_action_count(page: Page) -> int:
    count = 0
    for selector in PROFILE.selectors("response_action"):
        try:
            locator = page.locator(selector)
            for index in range(int(locator.count())):
                try:
                    if locator.nth(index).is_visible():
                        count += 1
                except Exception:
                    continue
        except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
            raise
        except Exception:
            continue
    return count


def _wait_response_footer_ready(
    page: Page,
    action_baseline: int,
    timeout: float = RESPONSE_FOOTER_TIMEOUT,
) -> None:
    if not PROFILE.selectors("response_action"):
        return
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


def _fresh_response_text(page: Page, baseline: int) -> str:
    try:
        return str(
            page.evaluate(
                _FRESH_RESPONSE_TEXT_JS,
                {"selector": _response_selector(), "baseline": baseline},
            )
            or ""
        ).strip()
    except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
        raise
    except Exception:
        return _fresh_response_text_fallback(page, baseline)


def _latest_response_text(page: Page) -> str:
    try:
        return str(page.evaluate(_LATEST_RESPONSE_TEXT_JS, _response_selector()) or "").strip()
    except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
        raise
    except Exception:
        # Provider-local newest-first fallback; the generic tail-first
        # locate_response() would return a stale reply on StepFun.
        return _latest_response_text_fallback(page)


def _final_text(page: Page, baseline: int) -> str:
    raw = _fresh_response_text(page, baseline)
    if not raw and baseline == 0:
        raw = _latest_response_text(page)
    raw = _normalize_final_json_tool_reply(raw)
    if raw:
        controls.confirm_control(PROVIDER_ID, controls.CONTROL_RESPONSE)
        return raw
    controls.reject_control(PROVIDER_ID, controls.CONTROL_RESPONSE)
    raise RuntimeError("Could not read the StepFun response")


def _fill_message(textarea: Locator, text: str) -> str:
    cancellation.check()
    textarea.click()
    cancellation.check()
    try:
        textarea.press("Control+A")
        textarea.press("Backspace")
    except Exception:
        pass
    try:
        textarea.evaluate(_SET_TEXTAREA_VALUE_JS, text)
    except Exception:
        textarea.fill(text)
    return text


def _composer_retains_text(
    textarea: Locator,
    submitted_text: str,
    *,
    settle_time: float = COMPOSER_SETTLE_TIME,
) -> bool:
    """Return true only after StepFun keeps text through late hydration."""
    ticks = max(1, int(max(0.0, settle_time) / COMPOSER_SETTLE_TICK))
    for _ in range(ticks):
        if not controls.control_has_text(textarea, submitted_text):
            return False
        cancellation.wait(COMPOSER_SETTLE_TICK)
    return controls.control_has_text(textarea, submitted_text)


def _fill_message_until_stable(textarea: Locator, text: str) -> str:
    """Fill StepFun's composer and survive late page hydration clears."""
    for attempt in range(max(1, COMPOSER_REFILL_ATTEMPTS)):
        submitted_text = _fill_message(textarea, text)
        if _composer_retains_text(textarea, submitted_text):
            return submitted_text
        if attempt + 1 < COMPOSER_REFILL_ATTEMPTS:
            cancellation.wait(COMPOSER_REFILL_DELAY)
    raise ControlMissing(
        "StepFun Chat input did not keep the complete message",
        stage=provider_flow.STAGE_SUBMISSION,
    )


def _control_text(control: Locator) -> str:
    try:
        return str(control.input_value() or "")
    except Exception:
        try:
            return str(control.inner_text() or "")
        except Exception:
            return ""


def _submission_started(
    page: Page,
    baseline: int,
    submitted_text: str,
) -> bool:
    try:
        if _response_count(page) > baseline:
            return True
        if _fresh_response_text(page, baseline):
            return True
        message_box = _message_box(page)
        input_empty = message_box is not None and not _control_text(message_box).strip()
        if input_empty:
            return True
        return controls.flow_matches(
            PROVIDER_ID,
            provider_flow.STAGE_SUBMISSION,
            provider_flow.FlowObservation(
                input_empty=input_empty,
                response_count_increased=False,
            ),
        )
    except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
        raise
    except Exception:
        return False


def _wait_submission_started(
    page: Page,
    baseline: int,
    submitted_text: str,
    timeout: float = SUBMIT_CONFIRM_TIMEOUT,
) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _submission_started(page, baseline, submitted_text):
            return True
        cancellation.wait(0.2)
    return False


def _submit(page: Page, textarea: Locator, baseline: int, submitted_text: str) -> SendAttempt:
    attempt = SendAttempt()
    button = _send_button(page, timeout=1.0, teach=False)
    if button is not None:
        attempt.submit("click", button.click)
        if _wait_submission_started(page, baseline, submitted_text):
            confirm_submission(attempt, PROVIDER_ID)
            return attempt
        cancellation.wait(0.6)
        # Double-submission guard (mirrors GLM): the confirmation watcher can
        # lose a race with a slow first submit. If any start signal is now
        # visible -- input cleared, response count moved, flow recorded --
        # the first click landed and a second forced click would post the
        # same message twice.
        if _submission_started(page, baseline, submitted_text):
            confirm_submission(attempt, PROVIDER_ID)
            return attempt
        retry_button = _send_button(page, timeout=2.0, teach=False)
        if retry_button is None:
            return attempt
        retry = SendAttempt()
        retry.submit("click", lambda: retry_button.click(force=True))
        if _wait_submission_started(page, baseline, submitted_text):
            confirm_submission(retry, PROVIDER_ID)
        return retry
    controls.reject_control(PROVIDER_ID, controls.CONTROL_SEND_BUTTON, page=page)
    raise ControlMissing(
        "StepFun Chat send button did not become ready",
        stage=provider_flow.STAGE_SUBMISSION,
    )


def _wait_late_response(
    page: Page,
    baseline: int,
    grace: float = TIMEOUT_GRACE,
    tick: float = 0.8,
) -> str:
    deadline = time.time() + max(0.0, grace)
    while time.time() < deadline:
        try:
            current = _fresh_response_text(page, baseline)
            if current:
                return _final_text(page, baseline)
        except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
            raise
        except Exception:
            pass
        cancellation.wait(tick)
    return ""


@controls.revival_send(PROVIDER_ID)
def chat(
    page: Page,
    text: str,
    response_timeout: float = 300.0,
    stable_ticks: int = 4,
    tick: float = 0.8,
    min_wait: float = 1.5,
) -> str:
    """Send one message and return the next StepFun answer."""
    cancellation.check()
    if not text.strip():
        raise ValueError("StepFun message cannot be blank")
    wait_ready(page)

    baseline = _response_count(page)
    action_baseline = _response_action_count(page)
    with send_loop.response_watch(page, PROVIDER_ID):
        textarea = _message_box(page, teach=True)
        if textarea is None:
            controls.reject_control(
                PROVIDER_ID,
                controls.CONTROL_MESSAGE_BOX,
                page=page,
            )
            raise ControlMissing("StepFun Chat input is not visible")
        try:
            submitted_text = _fill_message_until_stable(textarea, text)
        except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
            raise
        except Exception:
            controls.reject_control(PROVIDER_ID, controls.CONTROL_MESSAGE_BOX)
            raise
        controls.confirm_control(PROVIDER_ID, controls.CONTROL_MESSAGE_BOX)
        cancellation.wait(0.3)

        attempt = _submit(page, textarea, baseline, submitted_text)
        if not attempt.confirmed:
            if attempt.method == "click" and attempt.action_error is not None:
                controls.reject_control(PROVIDER_ID, controls.CONTROL_SEND_BUTTON)
            raise SubmissionUncertain("StepFun submission status is uncertain")

        ctx = send_loop.ProviderSendContext(
            page=page,
            provider_id=PROVIDER_ID,
            display_name="StepFun",
            sent_at=time.time(),
        )
        deadline = ctx.sent_at + response_timeout
        stable = 0
        while time.time() < deadline:
            cancellation.wait(tick)
            current = _fresh_response_text(page, baseline)
            if not current:
                continue
            confirm_submission(attempt, PROVIDER_ID)
            ctx.appeared = True
            same = ctx.same_as_last(current)
            observation = provider_flow.FlowObservation(
                response_stable=same,
                response_nonempty=True,
            )
            ctx.trace.add(observation)
            if same and (time.time() - ctx.sent_at) >= min_wait:
                stable += 1
                is_json_tool = _is_json_tool_reply(current)
                repairable_json_tool = False
                if _looks_like_json_tool_reply(current) and not is_json_tool:
                    repairable_json_tool = bool(
                        _repair_missing_trailing_braces_json_tool_reply(current)
                    )
                    if not repairable_json_tool and stable < stable_ticks:
                        continue
                if stable >= JSON_TOOL_STABLE_TICKS and is_json_tool:
                    _wait_response_footer_ready(page, action_baseline)
                    return send_loop.read_completion(
                        ctx,
                        lambda: _final_text(page, baseline),
                    )
                if repairable_json_tool and stable < stable_ticks:
                    continue
                if repairable_json_tool:
                    _wait_response_footer_ready(page, action_baseline)
                    return send_loop.read_completion(
                        ctx,
                        lambda: _final_text(page, baseline),
                    )
                ready = send_loop.completion_ready(
                    ctx,
                    observation,
                    built_in_ready=stable >= stable_ticks,
                )
                if ready:
                    _wait_response_footer_ready(page, action_baseline)
                    return send_loop.read_completion(
                        ctx,
                        lambda: _final_text(page, baseline),
                    )
            else:
                stable = 0
                ctx.last = current

        late = _wait_late_response(page, baseline, grace=TIMEOUT_GRACE, tick=tick)
        if late:
            confirm_submission(attempt, PROVIDER_ID)
            _wait_response_footer_ready(page, action_baseline)
            return late
        if ctx.appeared and ctx.last:
            _wait_response_footer_ready(page, action_baseline)
            return _final_text(page, baseline)
        recovered = controls.recover_response(
            page,
            PROVIDER_ID,
            lambda: _final_text(page, baseline),
        )
        if recovered is not None:
            confirm_submission(attempt, PROVIDER_ID)
            _wait_response_footer_ready(page, action_baseline)
            return recovered
        if not attempt.confirmed:
            if attempt.method == "click" and attempt.action_error is not None:
                controls.reject_control(PROVIDER_ID, controls.CONTROL_SEND_BUTTON)
            raise SubmissionUncertain("StepFun submission status is uncertain")
        raise ResponseMissing(f"StepFun response timed out after {response_timeout:.0f}s")
