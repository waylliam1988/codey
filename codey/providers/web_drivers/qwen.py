"""Qwen Studio web driver using selectors verified against the live page."""

from __future__ import annotations

import time

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page

from codey.runtime import cancellation
from codey.providers import controls as controls
from codey.providers import flow as provider_flow
from codey.providers import send_loop as send_loop
from codey.providers.profiles import get_profile
from codey.providers.diagnostics import ControlMissing
from codey.toolchain.json_reply import (
    is_json_tool_reply as _is_json_tool_reply,
    normalize_final_json_tool_reply as _normalize_final_json_tool_reply,
)
from codey.providers.timeouts import navigation_timeout_ms, remaining, start_deadline
from codey.providers.web_drivers import common as driver_common
from codey.providers.submission import (
    SendAttempt,
    confirm_submission,
)
from codey.automation.web_clipboard import copy_action_text

PROVIDER_ID = "qwen"
PROFILE = get_profile(PROVIDER_ID)
QWEN_URL = "https://chat.qwen.ai/"
STOP_ACTIVE = PROFILE.selector("stop_button")
RESPONSE_MESSAGE = PROFILE.selector("response_message")
EMPTY_RESPONSE = PROFILE.selector("empty_response")
RESPONSE_COPY = PROFILE.selector("copy_button")
REGENERATE = PROFILE.selector("regenerate_button")
PREFERENCE_CHOICE = PROFILE.selector("preference_choice")

READY_TIMEOUT = 90.0
TIMEOUT_GRACE = 60.0
SEND_TIMEOUT = 30.0
MODEL_SELECTOR_STABLE_READS = 2
COMPOSER_REFILL_ATTEMPTS = 3
COMPOSER_REFILL_DELAY = 0.4
COMPOSER_READY_TIMEOUT = 10.0
COMPOSER_READY_TICK = 0.1
COMPOSER_ACCEPT_TIMEOUT = 3.0
HOME_SUBMIT_HYDRATION_DELAY = 2.5
SUBMIT_CONFIRM_TIMEOUT = 15.0
COPY_READY_TIMEOUT = 10.0
PREFERENCE_TIMEOUT = 15.0
REGENERATE_START_TIMEOUT = 15.0
JSON_TOOL_STABLE_TICKS = 2

_MODEL_SELECTOR_TEXT_JS = r"""
() => {
  const visible = (el) => {
    if (!el) return false;
    const rect = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return rect.width > 0
      && rect.height > 0
      && style.visibility !== 'hidden'
      && style.display !== 'none';
  };
  const selectors = [
    '#qwen-chat-header-left .ant-dropdown-trigger',
    '#qwen-chat-header-left [class*="model-selector"]',
    '.header-left .ant-dropdown-trigger',
  ];
  for (const selector of selectors) {
    for (const element of document.querySelectorAll(selector)) {
      if (!visible(element)) continue;
      const text = String(element.innerText || element.textContent || '')
        .replace(/\s+/g, ' ')
        .trim();
      if (text) return text;
    }
  }
  return '';
}
"""


def _visible_locator(page: Page, selector: str) -> Locator | None:
    return controls.visible_locator(page, selector)


def _message_box(page: Page, *, teach: bool = False) -> Locator | None:
    return driver_common.message_box(PROVIDER_ID, PROFILE, page, teach=teach)


def _fill_message(page: Page, textarea: Locator, text: str) -> str:
    """Enter text through Qwen's keyboard path so its UI state is updated."""
    cancellation.check()
    textarea.click()
    cancellation.check()
    textarea.press("Control+A")
    textarea.press("Backspace")
    cancellation.check()
    page.keyboard.insert_text(text)
    # Qwen may display inserted text before its controlled composer accepts it.
    # A real trailing key commits the full value; keeping it avoids a batched
    # add/remove pair that can leave the website's internal state empty.
    textarea.press("End")
    textarea.press("Space")
    return f"{text} "


def _composer_value(textarea: Locator) -> str:
    try:
        return str(textarea.input_value() or "")
    except Exception:
        try:
            return str(textarea.inner_text() or "")
        except Exception:
            return ""


def _composer_is_interactive(textarea: Locator) -> bool:
    try:
        return bool(textarea.is_visible() and textarea.is_enabled())
    except Exception:
        return False


def _wait_composer_ready(
    page: Page,
    timeout: float = COMPOSER_READY_TIMEOUT,
    *,
    teach: bool = False,
) -> Locator:
    """Return Qwen's composer once the page can accept a new message."""
    deadline = time.time() + max(0.0, timeout)
    while True:
        cancellation.check()
        textarea = _message_box(page, teach=teach)
        if (
            textarea is not None
            and _composer_is_interactive(textarea)
            and _visible_locator(page, STOP_ACTIVE) is None
        ):
            return textarea
        remaining_time = deadline - time.time()
        if remaining_time <= 0:
            break
        cancellation.wait(min(COMPOSER_READY_TICK, remaining_time))
    raise TimeoutError("Qwen Studio chat input is not ready")


def _composer_accepts_submission(
    page: Page,
    textarea: Locator,
    submitted_text: str,
    *,
    timeout: float = COMPOSER_ACCEPT_TIMEOUT,
) -> bool:
    """Return true when Qwen keeps text and enables submission."""
    deadline = time.time() + max(0.0, timeout)
    while True:
        if not _composer_has_submitted_text(textarea, submitted_text):
            return False
        if _send_button(page, timeout=0.0, require_enabled=True, teach=False) is not None:
            return _composer_has_submitted_text(textarea, submitted_text)
        remaining_time = deadline - time.time()
        if remaining_time <= 0:
            return False
        cancellation.wait(min(COMPOSER_READY_TICK, remaining_time))


def _composer_has_submitted_text(textarea: Locator, submitted_text: str) -> bool:
    actual = _composer_value(textarea)
    expected = str(submitted_text or "")
    if not expected:
        return False
    if actual == expected:
        return True
    return bool(actual.rstrip()) and actual.rstrip() == expected.rstrip()


def _fill_message_until_stable(page: Page, textarea: Locator, text: str) -> str:
    """Fill Qwen's controlled composer and survive late page hydration clears."""
    for attempt in range(max(1, COMPOSER_REFILL_ATTEMPTS)):
        submitted_text = _fill_message(page, textarea, text)
        if _composer_accepts_submission(page, textarea, submitted_text):
            return submitted_text
        if attempt + 1 < COMPOSER_REFILL_ATTEMPTS:
            cancellation.wait(COMPOSER_REFILL_DELAY)
    raise ControlMissing("Qwen Studio chat input did not keep the complete message")


def _model_selector_text(page: Page) -> str:
    try:
        return str(page.evaluate(_MODEL_SELECTOR_TEXT_JS) or "").strip()
    except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
        raise
    except Exception:
        return ""


def _send_button(
    page: Page,
    *,
    timeout: float = 0.0,
    require_enabled: bool = True,
    teach: bool = False,
) -> Locator | None:
    message_box = _message_box(page)
    control = controls.locate_control(
        page,
        PROVIDER_ID,
        controls.CONTROL_SEND_BUTTON,
        PROFILE.selectors("send_button"),
        timeout=timeout,
        require_enabled=require_enabled,
        teach=False,
        anchor=message_box,
    )
    if control is not None:
        return control
    if require_enabled:
        control = _qwen_enabled_send_button(page)
        if control is not None:
            return control
    if teach:
        return controls.locate_control(
            page,
            PROVIDER_ID,
            controls.CONTROL_SEND_BUTTON,
            PROFILE.selectors("send_button"),
            timeout=0.0,
            require_enabled=require_enabled,
            teach=True,
            anchor=message_box,
        )
    return None


def _qwen_enabled_send_button(page: Page) -> Locator | None:
    """Return Qwen's visible enabled send button without broad discovery."""
    try:
        buttons = page.locator("button.send-button")
        count = int(buttons.count())
    except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
        raise
    except Exception:
        return None
    for index in range(count - 1, -1, -1):
        candidate = buttons.nth(index)
        try:
            if not candidate.is_visible() or not candidate.is_enabled():
                continue
            state = candidate.evaluate(
                """el => ({
                    disabled: !!el.disabled,
                    ariaDisabled: el.getAttribute('aria-disabled') || '',
                    className: String(el.className || '')
                })"""
            )
        except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
            raise
        except Exception:
            continue
        class_name = str((state or {}).get("className") or "")
        aria_disabled = str((state or {}).get("ariaDisabled") or "").lower()
        if (
            not (state or {}).get("disabled")
            and aria_disabled != "true"
            and "disabled" not in class_name.split()
        ):
            return candidate
    return None


def wait_ready(page: Page, timeout: float = READY_TIMEOUT) -> None:
    cancellation.check()
    deadline = time.time() + timeout
    stable_text = ""
    stable_reads = 0
    while time.time() < deadline:
        if _message_box(page) is not None:
            model_text = _model_selector_text(page)
            if model_text and model_text == stable_text:
                stable_reads += 1
                if stable_reads >= MODEL_SELECTOR_STABLE_READS:
                    return
            elif model_text:
                stable_text = model_text
                stable_reads = 1
            else:
                stable_text = ""
                stable_reads = 0
        cancellation.wait(0.4)
    if _message_box(page, teach=True) is not None:
        model_text = _model_selector_text(page)
        if model_text and model_text == stable_text:
            stable_reads += 1
            if stable_reads >= MODEL_SELECTOR_STABLE_READS:
                return
    raise TimeoutError("Qwen Studio did not finish loading its model selector. Are you logged in?")


def _is_qwen_home(page: Page) -> bool:
    try:
        return str(page.url or "").split("?", 1)[0].rstrip("/") == QWEN_URL.rstrip("/")
    except Exception:
        return False


def _wait_home_submit_hydrated(
    page: Page,
    timeout: float = HOME_SUBMIT_HYDRATION_DELAY,
) -> None:
    """Wait out Qwen's homepage false-ready state before the first submit."""
    if not _is_qwen_home(page):
        return
    delay = max(0.0, float(timeout))
    if delay > 0:
        cancellation.wait(delay)


def new_chat(page: Page, timeout: float | None = None) -> None:
    cancellation.check()
    deadline = start_deadline(timeout)
    try:
        page.goto(
            QWEN_URL,
            wait_until="domcontentloaded",
            timeout=navigation_timeout_ms(deadline),
        )
    except PlaywrightError as exc:
        if "net::ERR_ABORTED" not in str(exc):
            raise
    if deadline is None:
        wait_ready(page)
        _wait_composer_ready(page)
        _wait_home_submit_hydrated(page)
    else:
        wait_ready(page, timeout=remaining(deadline, READY_TIMEOUT))
        _wait_composer_ready(page, timeout=remaining(deadline, COMPOSER_READY_TIMEOUT))
        _wait_home_submit_hydrated(
            page,
            timeout=min(
                HOME_SUBMIT_HYDRATION_DELAY,
                remaining(deadline, HOME_SUBMIT_HYDRATION_DELAY),
            ),
        )


def _response_count(page: Page) -> int:
    return driver_common.response_count(PROVIDER_ID, PROFILE, page)


def _last_text(page: Page) -> str:
    response = controls.locate_response(page, PROVIDER_ID, PROFILE.selectors("response"))
    if response is None:
        return ""
    try:
        return response.inner_text().strip()
    except Exception:
        return ""


def _copy_last_text(page: Page) -> str:
    """Use Qwen's own copy action to recover source text before Markdown rendering."""
    responses = page.locator(RESPONSE_MESSAGE)
    if not responses.count():
        return ""
    response = responses.last
    copy_deadline = time.time() + COPY_READY_TIMEOUT
    copy_button = None
    while time.time() < copy_deadline:
        locator = response.locator(RESPONSE_COPY).last
        if locator.count() and locator.is_visible():
            copy_button = locator
            break
        cancellation.wait(0.2)
    if copy_button is None:
        return ""
    cancellation.check()
    return copy_action_text(page, copy_button, origin=QWEN_URL)


def _empty_response_visible(page: Page) -> bool:
    responses = page.locator(RESPONSE_MESSAGE)
    if not responses.count():
        return False
    empty = responses.last.locator(EMPTY_RESPONSE).last
    return bool(empty.count() and empty.is_visible())


def _regenerate_empty_response(page: Page) -> bool:
    responses = page.locator(RESPONSE_MESSAGE)
    if not responses.count():
        return False
    regenerate = responses.last.locator(REGENERATE).last
    if not regenerate.count() or not regenerate.is_visible():
        return False
    cancellation.check()
    regenerate.click()

    deadline = time.time() + REGENERATE_START_TIMEOUT
    while time.time() < deadline:
        if _visible_locator(page, STOP_ACTIVE) is not None:
            return True
        if not _empty_response_visible(page):
            return True
        cancellation.wait(0.2)
    return False


def _resolve_preference(page: Page) -> bool:
    """Select the first answer when Qwen pauses the chat for A/B feedback."""
    choices = page.locator(PREFERENCE_CHOICE)
    choice = None
    for index in range(choices.count()):
        candidate = choices.nth(index)
        try:
            if candidate.is_visible():
                choice = candidate
                break
        except Exception:
            continue
    if choice is None:
        return False

    cancellation.check()
    choice.click()
    deadline = time.time() + PREFERENCE_TIMEOUT
    while time.time() < deadline:
        if _visible_locator(page, PREFERENCE_CHOICE) is None:
            return True
        cancellation.wait(0.2)
    raise TimeoutError("Qwen Studio preference selection did not close")


def _final_text(page: Page) -> str:
    _resolve_preference(page)
    try:
        raw = _copy_last_text(page)
        dom = _last_text(page)
        if not raw:
            raw = dom
        elif _is_json_tool_reply(dom) and not _is_json_tool_reply(raw):
            raw = dom
        raw = _normalize_final_json_tool_reply(raw)
    except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
        raise
    except Exception:
        controls.reject_control(PROVIDER_ID, controls.CONTROL_RESPONSE)
        raise
    if not raw:
        controls.reject_control(PROVIDER_ID, controls.CONTROL_RESPONSE)
        raise RuntimeError("Could not read the Qwen Studio response")
    controls.confirm_control(PROVIDER_ID, controls.CONTROL_RESPONSE)
    return raw


def _generation_complete(page: Page) -> bool:
    return _send_button(page, require_enabled=False) is not None and _visible_locator(page, STOP_ACTIVE) is None


def _submission_started(page: Page, baseline: int, submitted_text: str = "") -> bool:
    try:
        count = _response_count(page)
        if _visible_locator(page, STOP_ACTIVE) is not None or count > baseline:
            return True
        if not submitted_text:
            return False
        return controls.flow_matches(
            PROVIDER_ID,
            provider_flow.STAGE_SUBMISSION,
            provider_flow.FlowObservation(
                response_count_increased=count > baseline,
            ),
        )
    except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
        raise
    except Exception:
        return False


def _submit(page: Page, baseline: int, submitted_text: str = "") -> SendAttempt:
    send = _send_button(page, timeout=SEND_TIMEOUT, teach=True)
    if send is None:
        controls.reject_control(PROVIDER_ID, controls.CONTROL_SEND_BUTTON, page=page)
        raise ControlMissing(
            "Qwen Studio send button did not become ready",
            stage=provider_flow.STAGE_SUBMISSION,
        )

    if submitted_text:
        textarea = _message_box(page)
        if textarea is None or not _composer_has_submitted_text(textarea, submitted_text):
            controls.reject_control(PROVIDER_ID, controls.CONTROL_MESSAGE_BOX, page=page)
            raise ControlMissing(
                "Qwen Studio chat input lost the message before submission",
                stage=provider_flow.STAGE_SUBMISSION,
            )

    attempt = SendAttempt()
    attempt.submit("click", send.click)
    confirm_deadline = time.time() + SUBMIT_CONFIRM_TIMEOUT
    while time.time() < confirm_deadline:
        if _submission_started(page, baseline, submitted_text):
            confirm_submission(attempt, PROVIDER_ID)
            return attempt
        cancellation.wait(0.2)
    return attempt


def _wait_late_response(
    page: Page,
    baseline: int,
    baseline_text: str = "",
    grace: float = TIMEOUT_GRACE,
    tick: float = 0.8,
) -> str:
    def _ready() -> str:
        count = _response_count(page)
        current = _last_text(page) if count else ""
        if current and (count > baseline or current != baseline_text):
            if _generation_complete(page):
                return _final_text(page)
        return ""

    return driver_common.poll_late_response(_ready, grace=grace, tick=tick)


def _chat(
    page: Page,
    text: str,
    response_timeout: float = 300.0,
    stable_ticks: int = 2,
    tick: float = 0.8,
    min_wait: float = 1.5,
) -> str:
    """Send one message and return the final answer text from Qwen Studio."""
    cancellation.check()
    wait_ready(page)
    textarea = _wait_composer_ready(page, teach=True)

    baseline = _response_count(page)
    baseline_text = _last_text(page) if baseline else ""

    with send_loop.response_watch(page, PROVIDER_ID):
        try:
            submitted_text = _fill_message_until_stable(page, textarea, text)
        except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
            raise
        except Exception:
            controls.reject_control(PROVIDER_ID, controls.CONTROL_MESSAGE_BOX)
            raise
        controls.confirm_control(PROVIDER_ID, controls.CONTROL_MESSAGE_BOX)
        attempt = _submit(page, baseline, submitted_text)

        ctx = send_loop.ProviderSendContext(
            page=page,
            provider_id=PROVIDER_ID,
            display_name="Qwen Studio",
            sent_at=time.time(),
        )
        deadline = time.time() + max(0.0, response_timeout)
        regenerated = False
        while time.time() < deadline:
            cancellation.wait(tick)
            if _empty_response_visible(page) and _generation_complete(page):
                confirm_submission(attempt, PROVIDER_ID)
                if regenerated or not _regenerate_empty_response(page):
                    raise RuntimeError("Qwen Studio returned an empty response")
                regenerated = True
                ctx.reset_text_progress(sent_at=time.time())
                continue
            count = _response_count(page)
            current = _last_text(page) if count else ""
            if count <= baseline and current == baseline_text:
                continue
            confirm_submission(attempt, PROVIDER_ID)
            ctx.appeared = True
            if not current:
                ctx.stable = 0
                continue
            same = ctx.same_as_last(current)
            stop_visible = _visible_locator(page, STOP_ACTIVE) is not None
            observation = provider_flow.FlowObservation(
                stop_visible=stop_visible,
                stop_hidden=not stop_visible,
                response_stable=same,
                response_nonempty=bool(current),
            )
            ctx.record_response(current, observation)
            if (
                ctx.stable >= JSON_TOOL_STABLE_TICKS
                and (time.time() - ctx.sent_at) >= min_wait
                and _is_json_tool_reply(current)
            ):
                return send_loop.read_completion(
                    ctx,
                    lambda: _final_text(page),
                )
            if ctx.stable >= stable_ticks and (time.time() - ctx.sent_at) >= min_wait:
                completion_ready = send_loop.completion_ready(
                    ctx,
                    observation,
                    built_in_ready=_generation_complete(page),
                    allow_recovery=attempt.confirmed,
                )
                if completion_ready:
                    return send_loop.read_completion(
                        ctx,
                        lambda: _final_text(page),
                    )

        action = ""
        if attempt.action_error is not None:
            action = f"; click failed with {type(attempt.action_error).__name__}"
        return send_loop.recover_or_raise(
            ctx,
            attempt,
            read_final=lambda: _final_text(page),
            read_late=lambda: _wait_late_response(
                page,
                baseline,
                baseline_text=baseline_text,
                grace=TIMEOUT_GRACE,
                tick=tick,
            ),
            response_timeout=response_timeout,
            uncertain_message=f"Qwen Studio submission status is uncertain{action}",
        )


@controls.revival_send(PROVIDER_ID)
def chat(
    page: Page,
    text: str,
    response_timeout: float = 300.0,
    stable_ticks: int = 2,
    tick: float = 0.8,
    min_wait: float = 1.5,
) -> str:
    return _chat(page, text, response_timeout, stable_ticks, tick, min_wait)
