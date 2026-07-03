"""Qwen Studio web driver using selectors verified against the live page."""

from __future__ import annotations

import time

from playwright.sync_api import Locator, Page

from codey import cancellation, provider_controls as controls
from codey.provider_profiles import get_profile
from codey.provider_submission import (
    SendAttempt,
    SubmissionUncertain,
    confirm_submission,
)
from codey.web_clipboard import copy_action_text

PROVIDER_ID = "qwen"
PROFILE = get_profile(PROVIDER_ID)
QWEN_URL = "https://chat.qwen.ai/"
INPUT = PROFILE.selector("message_box")
SEND_READY, SEND_BUTTON = PROFILE.selectors("send_button")
STOP_ACTIVE = PROFILE.selector("stop_button")
RESPONSE_MESSAGE = PROFILE.selector("response_message")
ANSWER = PROFILE.combined("response")
EMPTY_RESPONSE = PROFILE.selector("empty_response")
RESPONSE_COPY = PROFILE.selector("copy_button")
REGENERATE = PROFILE.selector("regenerate_button")
PREFERENCE_CHOICE = PROFILE.selector("preference_choice")

READY_TIMEOUT = 90.0
TIMEOUT_GRACE = 60.0
SEND_TIMEOUT = 30.0
INPUT_SETTLE_TIME = 1.5
SUBMIT_CONFIRM_TIMEOUT = 15.0
COPY_READY_TIMEOUT = 10.0
PREFERENCE_TIMEOUT = 15.0
REGENERATE_START_TIMEOUT = 15.0


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


def _fill_message(page: Page, textarea: Locator, text: str) -> None:
    """Enter text through Qwen's keyboard path so its UI state is updated."""
    cancellation.check()
    textarea.click()
    cancellation.check()
    textarea.press("Control+A")
    textarea.press("Backspace")
    cancellation.check()
    page.keyboard.insert_text(text)


def _send_button(
    page: Page,
    *,
    timeout: float = 0.0,
    require_enabled: bool = True,
    teach: bool = False,
) -> Locator | None:
    message_box = _message_box(page)
    return controls.locate_control(
        page,
        PROVIDER_ID,
        controls.CONTROL_SEND_BUTTON,
        PROFILE.selectors("send_button"),
        timeout=timeout,
        require_enabled=require_enabled,
        teach=teach,
        anchor=message_box,
    )


def wait_ready(page: Page, timeout: float = READY_TIMEOUT) -> None:
    cancellation.check()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _message_box(page) is not None:
            return
        cancellation.wait(0.4)
    if _message_box(page, teach=True) is not None:
        return
    raise TimeoutError("Qwen Studio chat input did not appear. Are you logged in?")


def new_chat(page: Page) -> None:
    cancellation.check()
    page.goto(QWEN_URL, wait_until="domcontentloaded", timeout=60000)
    wait_ready(page)


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
    raw = _copy_last_text(page)
    if not raw:
        raw = _last_text(page)
    if not raw:
        raise RuntimeError("Could not read the Qwen Studio response")
    controls.confirm_control(PROVIDER_ID, controls.CONTROL_RESPONSE)
    return raw


def _generation_complete(page: Page) -> bool:
    return _send_button(page, require_enabled=False) is not None and _visible_locator(page, STOP_ACTIVE) is None


def _submission_started(page: Page, baseline: int, submitted_text: str = "") -> bool:
    try:
        if _visible_locator(page, STOP_ACTIVE) is not None or _response_count(page) > baseline:
            return True
        if not submitted_text:
            return False
        message_box = _message_box(page)
        return message_box is not None and not controls.control_has_text(message_box, submitted_text)
    except cancellation.TaskCancelled:
        raise
    except Exception:
        return False


def _submit(page: Page, baseline: int, submitted_text: str = "") -> SendAttempt:
    send = _send_button(page, timeout=SEND_TIMEOUT, teach=True)
    if send is None:
        raise TimeoutError("Qwen Studio send button did not become ready")

    attempt = SendAttempt()
    cancellation.wait(INPUT_SETTLE_TIME)
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
    last = ""
    while time.time() < deadline:
        try:
            count = _response_count(page)
            current = _last_text(page) if count else ""
            if current and (count > baseline or current != baseline_text):
                last = current
                if _generation_complete(page):
                    return _final_text(page)
        except cancellation.TaskCancelled:
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
    controls.start_response_watch(page, PROVIDER_ID)

    textarea = _message_box(page, teach=True)
    if textarea is None:
        raise TimeoutError("Qwen Studio chat input is not visible")
    _fill_message(page, textarea, text)
    if controls.control_has_text(textarea, text):
        controls.confirm_control(PROVIDER_ID, controls.CONTROL_MESSAGE_BOX)
    attempt = _submit(page, baseline, text)

    sent_at = time.time()
    deadline = sent_at + response_timeout
    last = ""
    stable = 0
    appeared = False
    regenerated = False
    while time.time() < deadline:
        cancellation.wait(tick)
        if _empty_response_visible(page) and _generation_complete(page):
            confirm_submission(attempt, PROVIDER_ID)
            if regenerated or not _regenerate_empty_response(page):
                raise RuntimeError("Qwen Studio returned an empty response")
            regenerated = True
            sent_at = time.time()
            deadline = sent_at + response_timeout
            last = ""
            stable = 0
            appeared = False
            continue
        count = _response_count(page)
        current = _last_text(page) if count else ""
        if count <= baseline and current == baseline_text:
            continue
        confirm_submission(attempt, PROVIDER_ID)
        appeared = True
        if not current:
            stable = 0
            continue
        if current == last:
            stable += 1
        else:
            stable = 0
            last = current
        if (
            stable >= stable_ticks
            and (time.time() - sent_at) >= min_wait
            and _generation_complete(page)
        ):
            return _final_text(page)

    late = _wait_late_response(
        page,
        baseline,
        baseline_text=baseline_text,
        grace=TIMEOUT_GRACE,
        tick=tick,
    )
    if late:
        confirm_submission(attempt, PROVIDER_ID)
        return late
    if appeared and last:
        return _final_text(page)
    recovered = controls.recover_response(page, PROVIDER_ID, lambda: _final_text(page))
    if recovered is not None:
        confirm_submission(attempt, PROVIDER_ID)
        return recovered
    controls.reject_control(PROVIDER_ID, controls.CONTROL_RESPONSE)
    if not attempt.confirmed:
        raise SubmissionUncertain("Qwen Studio submission status is uncertain")
    raise TimeoutError(f"Qwen Studio response timed out after {response_timeout:.0f}s")


def chat(
    page: Page,
    text: str,
    response_timeout: float = 300.0,
    stable_ticks: int = 2,
    tick: float = 0.8,
    min_wait: float = 1.5,
) -> str:
    try:
        return _chat(page, text, response_timeout, stable_ticks, tick, min_wait)
    finally:
        controls.stop_response_watch(page, PROVIDER_ID)
