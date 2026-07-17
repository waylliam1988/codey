"""Qwen Studio web driver using selectors verified against the live page."""

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
from codey.provider_profiles import get_profile
from codey.provider_diagnostics import ControlMissing, ResponseMissing
from codey.provider_timeouts import navigation_timeout_ms, remaining, start_deadline
from codey.provider_submission import (
    SendAttempt,
    confirm_submission,
)
from codey.web_clipboard import copy_action_text

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
COMPOSER_SETTLE_TIME = 1.5
SUBMIT_CONFIRM_TIMEOUT = 15.0
COPY_READY_TIMEOUT = 10.0
PREFERENCE_TIMEOUT = 15.0
REGENERATE_START_TIMEOUT = 15.0
MAX_STALLED_RESPONSE_RETRIES = 1

_BOOTSTRAP_READY_JS = r"""
() => {
  if (window.__qwenComposerReady) return true;
  const modelReady = performance.getEntriesByType('resource').some((entry) => {
    try {
      const url = new URL(entry.name);
      const status = Number(entry.responseStatus || 0);
      return url.origin === location.origin
        && url.pathname === '/api/v2/models/'
        && entry.responseEnd > 0
        && (status === 0 || (status >= 200 && status < 300));
    } catch (_) {
      return false;
    }
  });
  if (modelReady) window.__qwenComposerReady = true;
  return modelReady;
}
"""

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
    return controls.locate_control(
        page,
        PROVIDER_ID,
        controls.CONTROL_MESSAGE_BOX,
        PROFILE.selectors("message_box"),
        teach=teach,
    )


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


def _bootstrap_ready(page: Page) -> bool:
    """Return whether Qwen has loaded the model state used by its send handler."""
    try:
        return bool(page.evaluate(_BOOTSTRAP_READY_JS))
    except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
        raise
    except Exception:
        return False


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
        if _message_box(page) is not None and _bootstrap_ready(page):
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
    if _message_box(page, teach=True) is not None and _bootstrap_ready(page):
        model_text = _model_selector_text(page)
        if model_text and model_text == stable_text:
            stable_reads += 1
            if stable_reads >= MODEL_SELECTOR_STABLE_READS:
                return
    raise TimeoutError("Qwen Studio did not finish loading its model selector. Are you logged in?")


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
    else:
        wait_ready(page, timeout=remaining(deadline, READY_TIMEOUT))


def _response_count(page: Page) -> int:
    return controls.response_count(page, PROVIDER_ID, PROFILE.selectors("response"))


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
        if not raw:
            raw = _last_text(page)
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

    attempt = SendAttempt()
    # Qwen enables the button before its send closure receives the latest draft.
    cancellation.wait(COMPOSER_SETTLE_TIME)
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
    deadline = time.time() + max(0.0, grace)
    while time.time() < deadline:
        try:
            count = _response_count(page)
            current = _last_text(page) if count else ""
            if current and (count > baseline or current != baseline_text):
                if _generation_complete(page):
                    return _final_text(page)
        except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
            raise
        except Exception:
            pass
        cancellation.wait(tick)
    return ""


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

    baseline = _response_count(page)
    baseline_text = _last_text(page) if baseline else ""

    with send_loop.response_watch(page, PROVIDER_ID):
        textarea = _message_box(page, teach=True)
        if textarea is None:
            controls.reject_control(
                PROVIDER_ID, controls.CONTROL_MESSAGE_BOX, page=page
            )
            raise ControlMissing("Qwen Studio chat input is not visible")
        try:
            submitted_text = _fill_message(page, textarea, text)
        except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
            raise
        except Exception:
            controls.reject_control(PROVIDER_ID, controls.CONTROL_MESSAGE_BOX)
            raise
        if not controls.control_has_text(textarea, submitted_text):
            controls.reject_control(PROVIDER_ID, controls.CONTROL_MESSAGE_BOX)
            raise ControlMissing("Qwen Studio chat input did not accept the complete message")
        controls.confirm_control(PROVIDER_ID, controls.CONTROL_MESSAGE_BOX)
        attempt = _submit(page, baseline, submitted_text)

        ctx = send_loop.ProviderSendContext(
            page=page,
            provider_id=PROVIDER_ID,
            display_name="Qwen Studio",
            sent_at=time.time(),
        )
        deadline = ctx.sent_at + response_timeout
        regenerated = False
        while time.time() < deadline:
            cancellation.wait(tick)
            if _empty_response_visible(page) and _generation_complete(page):
                confirm_submission(attempt, PROVIDER_ID)
                if regenerated or not _regenerate_empty_response(page):
                    raise RuntimeError("Qwen Studio returned an empty response")
                regenerated = True
                ctx.reset_text_progress(sent_at=time.time())
                deadline = ctx.sent_at + response_timeout
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
    for attempt in range(MAX_STALLED_RESPONSE_RETRIES + 1):
        try:
            return _chat(page, text, response_timeout, stable_ticks, tick, min_wait)
        except TimeoutError as exc:
            if (
                attempt >= MAX_STALLED_RESPONSE_RETRIES
                or "response timed out" not in str(exc)
            ):
                raise
    raise ResponseMissing(f"Qwen Studio response timed out after {response_timeout:.0f}s")
