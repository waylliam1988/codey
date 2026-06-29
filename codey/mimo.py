"""Xiaomi MiMo Chat web driver using selectors verified against the live page."""

from __future__ import annotations

import time

from playwright.sync_api import Locator, Page

from codey import provider_controls as controls
from codey.web_clipboard import copy_action_text

PROVIDER_ID = "mimo"
MIMO_URL = "https://aistudio.xiaomimimo.com/#/c"
MIMO_ORIGIN = "https://aistudio.xiaomimimo.com"
INPUT = "textarea"
SEND_BUTTON = 'button[data-track-id="home_send_btn"], button[data-track-name="home_send_message"]'
ANSWER = '.markdown-prose, [class*="Markdown_markdown"]'

READY_TIMEOUT = 90.0
TIMEOUT_GRACE = 60.0
COPY_READY_TIMEOUT = 10.0
SUBMIT_CONFIRM_TIMEOUT = 15.0
SUBMIT_READY_TIMEOUT = 5.0


def _visible_locator(page: Page, selector: str) -> Locator | None:
    return controls.visible_locator(page, selector)


def _message_box(page: Page, *, teach: bool = False) -> Locator | None:
    return controls.locate_control(
        page,
        PROVIDER_ID,
        controls.CONTROL_MESSAGE_BOX,
        (INPUT,),
        teach=teach,
    )


def wait_ready(page: Page, timeout: float = READY_TIMEOUT) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _message_box(page) is not None:
            return
        time.sleep(0.4)
    if _message_box(page, teach=True) is not None:
        return
    raise TimeoutError("Xiaomi MiMo Chat input did not appear. Are you logged in?")


def new_chat(page: Page) -> None:
    page.goto(MIMO_URL, wait_until="domcontentloaded", timeout=60000)
    wait_ready(page)


def _response_count(page: Page) -> int:
    return page.locator(ANSWER).count()


def _last_text(page: Page) -> str:
    return page.evaluate(
        """({ answerSelector }) => {
          const answers = document.querySelectorAll(answerSelector);
          if (!answers.length) return '';
          const answer = answers[answers.length - 1];
          return (answer.innerText || answer.textContent || '').trim();
        }""",
        {"answerSelector": ANSWER},
    )


def _first_visible_button_after(page: Page, x_min: float, y_min: float, y_max: float) -> Locator | None:
    buttons = page.locator("button")
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


def _copy_last_text(page: Page) -> str:
    """Use MiMo's copy action to recover raw text before Markdown rendering."""
    responses = page.locator(ANSWER)
    if not responses.count():
        return ""
    response = responses.last
    deadline = time.time() + COPY_READY_TIMEOUT
    while time.time() < deadline:
        try:
            response.scroll_into_view_if_needed(timeout=1000)
            box = response.bounding_box()
        except Exception:
            box = None
        if box:
            action = _first_visible_button_after(
                page,
                x_min=max(0.0, float(box["x"]) - 8.0),
                y_min=float(box["y"] + box["height"]) - 8.0,
                y_max=float(box["y"] + box["height"]) + 90.0,
            )
            if action is not None:
                return copy_action_text(page, action, origin=MIMO_ORIGIN)
        time.sleep(0.2)
    return ""


def _final_text(page: Page) -> str:
    raw = _copy_last_text(page)
    if raw:
        return raw
    fallback = _last_text(page)
    if fallback:
        return fallback
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
            return textarea is not None and not textarea.input_value().strip()
        except Exception:
            return False
    return False


def _submit(page: Page, baseline: int, baseline_text: str = "", submitted_text: str = "") -> None:
    textarea = _message_box(page, teach=True)
    if textarea is None:
        raise TimeoutError("Xiaomi MiMo Chat input is not visible")

    button = _send_button(page)
    if button is not None:
        button.click()
        if _wait_submission_started(page, baseline, baseline_text, submitted_text):
            return
        raise TimeoutError("Xiaomi MiMo Chat did not submit the message")

    textarea.press("Enter")
    if _wait_submission_started(page, baseline, baseline_text, submitted_text):
        return

    if controls.can_teach():
        button = controls.request_teaching(
            page,
            PROVIDER_ID,
            controls.CONTROL_SEND_BUTTON,
            require_enabled=True,
        )
        if button is not None:
            button.click()
            if _wait_submission_started(page, baseline, baseline_text, submitted_text):
                return

    raise TimeoutError("Xiaomi MiMo Chat did not submit the message")


def _send_button(page: Page) -> Locator | None:
    return controls.locate_control(
        page,
        PROVIDER_ID,
        controls.CONTROL_SEND_BUTTON,
        (SEND_BUTTON,),
        timeout=SUBMIT_READY_TIMEOUT,
        require_enabled=True,
    )


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
        time.sleep(0.2)
    return False


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
                return _final_text(page)
        except Exception:
            pass
        time.sleep(tick)
    return last


def chat(
    page: Page,
    text: str,
    response_timeout: float = 300.0,
    stable_ticks: int = 4,
    tick: float = 0.8,
    min_wait: float = 1.5,
) -> str:
    """Send one message and return the final raw answer text from MiMo Chat."""
    wait_ready(page)

    baseline = _response_count(page)
    baseline_text = _last_text(page) if baseline else ""

    textarea = _message_box(page, teach=True)
    if textarea is None:
        raise TimeoutError("Xiaomi MiMo Chat input is not visible")
    textarea.click()
    textarea.fill(text)
    time.sleep(0.2)
    _submit(page, baseline, baseline_text, text)

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
        if stable >= stable_ticks and (time.time() - sent_at) >= min_wait:
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
    raise TimeoutError(f"Xiaomi MiMo response timed out after {response_timeout:.0f}s")
