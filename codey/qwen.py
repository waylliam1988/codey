"""Qwen Studio web driver using selectors verified against the live page."""

from __future__ import annotations

import time

from playwright.sync_api import Locator, Page

from codey.web_clipboard import copy_action_text

QWEN_URL = "https://chat.qwen.ai/"
INPUT = "textarea.message-input-textarea"
SEND_BUTTON = "button.send-button"
SEND_READY = "button.send-button:not(.disabled):not([disabled])"
STOP_ACTIVE = "button.stop-button:not(.disabled)"
RESPONSE_MESSAGE = ".chat-response-message"
ANSWER = ".response-message-content.phase-answer"
RESPONSE_COPY = ".response-message-footer .qwen-chat-package-comp-new-action-control-container-copy"
PREFERENCE_CHOICE = "button.smulti-make-better"

READY_TIMEOUT = 90.0
TIMEOUT_GRACE = 60.0
SEND_TIMEOUT = 30.0
SUBMIT_CONFIRM_TIMEOUT = 15.0
COPY_READY_TIMEOUT = 10.0
PREFERENCE_TIMEOUT = 15.0
MAX_SUBMIT_ATTEMPTS = 2


def _visible_locator(page: Page, selector: str) -> Locator | None:
    locator = page.locator(selector)
    for index in range(locator.count() - 1, -1, -1):
        candidate = locator.nth(index)
        try:
            if candidate.is_visible():
                return candidate
        except Exception:
            continue
    return None


def wait_ready(page: Page, timeout: float = READY_TIMEOUT) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _visible_locator(page, INPUT) is not None:
            return
        time.sleep(0.4)
    raise TimeoutError("Qwen Studio chat input did not appear. Are you logged in?")


def new_chat(page: Page) -> None:
    page.goto(QWEN_URL, wait_until="domcontentloaded", timeout=60000)
    wait_ready(page)


def _response_count(page: Page) -> int:
    return page.locator(ANSWER).count()


def _last_text(page: Page) -> str:
    return page.evaluate(
        r"""({ answerSelector }) => {
          const answers = document.querySelectorAll(answerSelector);
          if (!answers.length) return '';
          const answer = answers[answers.length - 1];
          return (answer.innerText || answer.textContent || '').trim();
        }""",
        {"answerSelector": ANSWER},
    )


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
        time.sleep(0.2)
    if copy_button is None:
        return ""
    return copy_action_text(page, copy_button, origin=QWEN_URL)


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

    choice.click()
    deadline = time.time() + PREFERENCE_TIMEOUT
    while time.time() < deadline:
        if _visible_locator(page, PREFERENCE_CHOICE) is None:
            return True
        time.sleep(0.2)
    raise TimeoutError("Qwen Studio preference selection did not close")


def _final_text(page: Page) -> str:
    _resolve_preference(page)
    raw = _copy_last_text(page)
    if not raw:
        raise RuntimeError("Could not read the raw Qwen Studio response")
    return raw


def _generation_complete(page: Page) -> bool:
    return _visible_locator(page, SEND_BUTTON) is not None and _visible_locator(page, STOP_ACTIVE) is None


def _submission_started(page: Page, baseline: int) -> bool:
    return _visible_locator(page, STOP_ACTIVE) is not None or _response_count(page) > baseline


def _submit(page: Page, baseline: int) -> None:
    for attempt in range(MAX_SUBMIT_ATTEMPTS):
        deadline = time.time() + SEND_TIMEOUT
        send = None
        while time.time() < deadline:
            send = _visible_locator(page, SEND_READY)
            if send is not None:
                break
            time.sleep(0.25)
        if send is None:
            raise TimeoutError("Qwen Studio send button did not become ready")

        if attempt and _submission_started(page, baseline):
            return
        if attempt == 0:
            time.sleep(0.75)
        send.click()
        confirm_deadline = time.time() + SUBMIT_CONFIRM_TIMEOUT
        while time.time() < confirm_deadline:
            if _submission_started(page, baseline):
                return
            time.sleep(0.2)
    raise TimeoutError("Qwen Studio did not submit the message")


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
        except Exception:
            pass
        time.sleep(tick)
    return ""


def chat(
    page: Page,
    text: str,
    response_timeout: float = 300.0,
    stable_ticks: int = 2,
    tick: float = 0.8,
    min_wait: float = 1.5,
) -> str:
    """Send one message and return the final answer text from Qwen Studio."""
    wait_ready(page)

    baseline = _response_count(page)
    baseline_text = _last_text(page) if baseline else ""

    textarea = _visible_locator(page, INPUT)
    if textarea is None:
        raise TimeoutError("Qwen Studio chat input is not visible")
    textarea.click()
    textarea.fill(text)
    _submit(page, baseline)

    sent_at = time.time()
    deadline = sent_at + response_timeout
    last = ""
    stable = 0
    appeared = False
    while time.time() < deadline:
        time.sleep(tick)
        count = _response_count(page)
        current = _last_text(page) if count else ""
        if count <= baseline and current == baseline_text:
            continue
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
        return late
    if appeared and last:
        return _final_text(page)
    raise TimeoutError(f"Qwen Studio response timed out after {response_timeout:.0f}s")
