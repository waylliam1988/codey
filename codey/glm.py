"""GLM web driver using selectors verified against the live page."""

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

PROVIDER_ID = "glm"
PROFILE = get_profile(PROVIDER_ID)
GLM_URL = "https://chatglm.cn/main/alltoolsdetail?lang=zh"
FORMAT_HINT = (
    "GLM page formatting override: when your answer is a local-runner JSON "
    "command, return one raw JSON object with ASCII U+0022 double quotes. "
    "Do not wrap it in markdown fences and add no other text. If the answer "
    "is normal prose, ignore this note."
)
SMART_TOOL_QUOTE_TRANSLATION = str.maketrans({
    "“": '"',
    "”": '"',
    "„": '"',
    "‟": '"',
})
THINKING_CONTENT = ".text-advance-thinking-content"
_FINAL_ANSWER_NODE_JS = r"""
(node, thinkingSelector) => {
  if (!(node instanceof Element)) return false;
  return node.closest(thinkingSelector) === null
    && node.querySelector(thinkingSelector) === null;
}
"""
_MARKDOWN_BODY = ".markdown-body"

READY_TIMEOUT = 90.0
RESPONSE_TIMEOUT_GRACE = 60.0
SEND_TIMEOUT = 30.0
SUBMIT_CONFIRM_TIMEOUT = 15.0


def prepare_prompt(text: str) -> str:
    return f"{text}\n\n{FORMAT_HINT}"


def normalize_tool_json_reply(text: str) -> str:
    """Repair GLM's occasional smart double quotes around JSON object keys."""

    stripped = text.lstrip()
    if not stripped.startswith(("{“", "{”", "{„", "{‟")):
        return text
    return text.translate(SMART_TOOL_QUOTE_TRANSLATION)


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
    return controls.locate_control(
        page,
        PROVIDER_ID,
        controls.CONTROL_SEND_BUTTON,
        PROFILE.selectors("send_button"),
        timeout=timeout,
        require_enabled=True,
        teach=teach,
        anchor=_message_box(page),
    )


def _generation_complete(page: Page) -> bool:
    return controls.visible_locator(page, PROFILE.selector("idle_button")) is not None


def wait_ready(page: Page, timeout: float = READY_TIMEOUT) -> None:
    cancellation.check()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _message_box(page) is not None:
            return
        cancellation.wait(0.4)
    if _message_box(page, teach=True) is not None:
        return
    raise TimeoutError("GLM chat input did not appear. Are you logged in?")


def new_chat(page: Page) -> None:
    cancellation.check()
    page.goto(GLM_URL, wait_until="domcontentloaded", timeout=60000)
    wait_ready(page)


def _response_count(page: Page) -> int:
    return controls.response_count(page, PROVIDER_ID, PROFILE.selectors("response"))


def _question_count(page: Page) -> int:
    try:
        return int(page.locator(PROFILE.selector("question")).count())
    except Exception:
        return 0


def _last_text(page: Page) -> str:
    response = controls.locate_response(
        page,
        PROVIDER_ID,
        PROFILE.selectors("response"),
    )
    if response is None:
        return ""
    try:
        if not response.evaluate(_FINAL_ANSWER_NODE_JS, THINKING_CONTENT):
            return ""
        markdown_parts = response.locator(_MARKDOWN_BODY).all_inner_texts()
        text = "\n".join(part.strip() for part in markdown_parts if part.strip()).strip()
        if text:
            return text
        return response.inner_text().strip()
    except Exception:
        return ""


def _final_text(page: Page) -> str:
    text = _last_text(page)
    if not text:
        raise RuntimeError("Could not read the GLM response")
    controls.confirm_control(PROVIDER_ID, controls.CONTROL_RESPONSE)
    return text


def _submission_started(
    page: Page,
    response_baseline: int,
    question_baseline: int,
    submitted_text: str,
    baseline_text: str = "",
) -> bool:
    try:
        current = _last_text(page) if response_baseline else ""
        if (
            _response_count(page) > response_baseline
            or _question_count(page) > question_baseline
            or (bool(current) and current != baseline_text)
        ):
            return True
        message_box = _message_box(page)
        return (
            message_box is not None
            and not controls.control_has_text(message_box, submitted_text)
        )
    except cancellation.TaskCancelled:
        raise
    except Exception:
        return False


def _submit(
    page: Page,
    response_baseline: int,
    question_baseline: int,
    submitted_text: str,
    baseline_text: str = "",
) -> SendAttempt:
    button = _send_button(page, timeout=SEND_TIMEOUT, teach=True)
    if button is None:
        raise TimeoutError("GLM send button did not become ready")

    attempt = SendAttempt()
    attempt.submit("click", button.click)
    deadline = time.time() + SUBMIT_CONFIRM_TIMEOUT
    while time.time() < deadline:
        if _submission_started(
            page,
            response_baseline,
            question_baseline,
            submitted_text,
            baseline_text,
        ):
            confirm_submission(attempt, PROVIDER_ID)
            break
        cancellation.wait(0.2)
    return attempt


def _wait_late_response(
    page: Page,
    baseline: int,
    *,
    baseline_text: str = "",
    grace: float = RESPONSE_TIMEOUT_GRACE,
    tick: float = 0.8,
) -> str:
    deadline = time.time() + max(0.0, grace)
    while time.time() < deadline:
        try:
            count = _response_count(page)
            current = _last_text(page) if count else ""
            if current and (count > baseline or current != baseline_text) and _generation_complete(page):
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
    stable_ticks: int = 3,
    tick: float = 0.8,
    min_wait: float = 1.5,
) -> str:
    cancellation.check()
    wait_ready(page)

    response_baseline = _response_count(page)
    baseline_text = _last_text(page) if response_baseline else ""
    question_baseline = _question_count(page)
    controls.start_response_watch(page, PROVIDER_ID)

    textarea = _message_box(page, teach=True)
    if textarea is None:
        raise TimeoutError("GLM chat input is not visible")
    cancellation.check()
    textarea.click()
    textarea.fill(text)
    if not controls.control_has_text(textarea, text):
        raise RuntimeError("GLM chat input did not accept the complete message")
    controls.confirm_control(PROVIDER_ID, controls.CONTROL_MESSAGE_BOX)

    attempt = _submit(
        page,
        response_baseline,
        question_baseline,
        text,
        baseline_text,
    )
    sent_at = time.time()
    deadline = sent_at + response_timeout
    last = ""
    stable = 0
    appeared = False

    while time.time() < deadline:
        cancellation.wait(tick)
        if _question_count(page) > question_baseline + 1:
            raise RuntimeError("GLM page submitted the message more than once")
        count = _response_count(page)
        current = _last_text(page) if count else ""
        if count <= response_baseline and current == baseline_text:
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
        response_baseline,
        baseline_text=baseline_text,
        grace=RESPONSE_TIMEOUT_GRACE,
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
        raise SubmissionUncertain("GLM submission status is uncertain")
    raise TimeoutError(f"GLM response timed out after {response_timeout:.0f}s")


def chat(
    page: Page,
    text: str,
    response_timeout: float = 300.0,
    stable_ticks: int = 3,
    tick: float = 0.8,
    min_wait: float = 1.5,
) -> str:
    try:
        cancellation.check()
        if not text.strip():
            raise ValueError("GLM message cannot be blank")
        prompt = prepare_prompt(text)
        reply = _chat(page, prompt, response_timeout, stable_ticks, tick, min_wait)
        return normalize_tool_json_reply(reply)
    finally:
        controls.stop_response_watch(page, PROVIDER_ID)
