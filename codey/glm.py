"""GLM web driver using selectors verified against the live page."""

from __future__ import annotations

import json
import time

from playwright.sync_api import Locator, Page

from codey import cancellation, provider_controls as controls
from codey.provider_profiles import get_profile
from codey.provider_diagnostics import ControlMissing, ResponseMissing
from codey.provider_timeouts import navigation_timeout_ms, remaining, start_deadline
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
    "Preserve source-code punctuation exactly and never use typographic smart "
    "quotes in code. Do not wrap the JSON in markdown fences and add no other "
    "text. If the answer is normal prose, ignore this note."
)
SMART_TOOL_QUOTES = frozenset({"“", "”", "„", "‟"})
SMART_SOURCE_QUOTES = frozenset({"‘", "’", "“", "”"})
SMART_SOURCE_QUOTE_TRANSLATION = str.maketrans({
    "‘": "'",
    "’": "'",
    "“": '"',
    "”": '"',
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
RATE_LIMIT_COOLDOWN = 10.0
RATE_LIMIT_TEXT = "请求过于频繁，请稍后再试"
RATE_LIMIT_RETRY_TEXT = "重新回答"


def prepare_prompt(text: str) -> str:
    return f"{text}\n\n{FORMAT_HINT}"


def normalize_tool_json_reply(text: str) -> str:
    """Repair structural smart quotes without changing quotes inside values."""

    try:
        json.loads(text)
    except (TypeError, ValueError):
        pass
    else:
        return _normalize_python_edit(text)

    stripped = text.lstrip()
    if not stripped.startswith("{") or not any(char in text for char in SMART_TOOL_QUOTES):
        return text

    candidate = _repair_structural_smart_quotes(text)
    try:
        json.loads(candidate)
    except (TypeError, ValueError):
        return text
    return _normalize_python_edit(candidate)


def _repair_structural_smart_quotes(text: str) -> str:
    """Build a structural-quote repair candidate for invalid JSON."""

    chars: list[str] = []
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if not in_string:
            if char == '"' or char in SMART_TOOL_QUOTES:
                chars.append('"')
                in_string = True
            else:
                chars.append(char)
            continue

        if escaped:
            chars.append(char)
            escaped = False
            continue
        if char == "\\":
            chars.append(char)
            escaped = True
            continue
        if char == '"':
            chars.append(char)
            in_string = False
            continue
        if char in SMART_TOOL_QUOTES:
            following = text[index + 1:].lstrip()[:1]
            if following in {":", ",", "}", "]"}:
                chars.append('"')
                in_string = False
                continue
        chars.append(char)
    return "".join(chars)


def _normalize_python_edit(text: str) -> str:
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return text
    original = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if not _is_python_edit_payload(payload):
        return text
    changed = _normalize_python_edit_content(payload)
    if not changed:
        return text
    rendered = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return rendered if rendered != original else text


def _is_python_edit_payload(payload: object) -> bool:
    if not isinstance(payload, dict) or payload.get("tool") != "edit":
        return False
    args = payload.get("args")
    if not isinstance(args, dict) or not str(args.get("path") or "").lower().endswith(".py"):
        return False
    return True


def _normalize_python_edit_content(payload: dict) -> bool:
    args = payload.get("args")
    if not isinstance(args, dict):
        return False
    content = args.get("content")
    if not isinstance(content, str) or not any(char in content for char in SMART_SOURCE_QUOTES):
        return False
    candidate = content.translate(SMART_SOURCE_QUOTE_TRANSLATION)
    try:
        compile(content, "<glm-edit>", "exec")
        return False
    except SyntaxError:
        pass
    try:
        compile(candidate, "<glm-edit>", "exec")
    except SyntaxError:
        return False
    args["content"] = candidate
    return True


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


def new_chat(page: Page, timeout: float | None = None) -> None:
    cancellation.check()
    deadline = start_deadline(timeout)
    page.goto(
        GLM_URL,
        wait_until="domcontentloaded",
        timeout=navigation_timeout_ms(deadline),
    )
    if deadline is None:
        wait_ready(page)
    else:
        wait_ready(page, timeout=remaining(deadline, READY_TIMEOUT))


def _response_count(page: Page) -> int:
    return controls.response_count(page, PROVIDER_ID, PROFILE.selectors("response"))


def _question_count(page: Page) -> int:
    try:
        return int(page.locator(PROFILE.selector("question")).count())
    except Exception:
        return 0


def _submitted_question_count(page: Page, submitted_text: str) -> int:
    """Count question nodes whose prompt body equals this submission."""

    needle = submitted_text.strip()
    if not needle:
        return 0
    try:
        values = page.locator(PROFILE.selector("question")).all_inner_texts()
    except Exception:
        return 0
    if not isinstance(values, list):
        return 0
    count = 0
    for value in values:
        _label, separator, body = value.partition("\n")
        prompt = body if separator else value
        if prompt.strip() == needle:
            count += 1
    return count


def _rate_limit_visible(page: Page) -> bool:
    try:
        return RATE_LIMIT_TEXT in str(page.locator("body").inner_text(timeout=1000))
    except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
        raise
    except Exception:
        return False


def _click_rate_limit_retry(page: Page) -> bool:
    cancellation.wait(RATE_LIMIT_COOLDOWN)
    buttons = page.get_by_text(RATE_LIMIT_RETRY_TEXT, exact=True)
    try:
        count = buttons.count()
        for index in range(count - 1, -1, -1):
            candidate = buttons.nth(index)
            if candidate.is_visible():
                cancellation.check()
                candidate.click()
                return True
    except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
        raise
    except Exception:
        return False
    return False


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
        controls.reject_control(PROVIDER_ID, controls.CONTROL_RESPONSE)
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
    except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
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
        controls.reject_control(PROVIDER_ID, controls.CONTROL_SEND_BUTTON, page=page)
        raise ControlMissing("GLM send button did not become ready")

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
    stable_ticks: int = 3,
    tick: float = 0.8,
    min_wait: float = 1.5,
) -> str:
    cancellation.check()
    wait_ready(page)

    response_baseline = _response_count(page)
    baseline_text = _last_text(page) if response_baseline else ""
    question_baseline = _question_count(page)
    submitted_question_baseline = _submitted_question_count(page, text)
    controls.start_response_watch(page, PROVIDER_ID)

    textarea = _message_box(page, teach=True)
    if textarea is None:
        controls.reject_control(
            PROVIDER_ID, controls.CONTROL_MESSAGE_BOX, page=page
        )
        raise ControlMissing("GLM chat input is not visible")
    try:
        cancellation.check()
        textarea.click()
        textarea.fill(text)
    except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
        raise
    except Exception:
        controls.reject_control(PROVIDER_ID, controls.CONTROL_MESSAGE_BOX)
        raise
    if not controls.control_has_text(textarea, text):
        controls.reject_control(PROVIDER_ID, controls.CONTROL_MESSAGE_BOX)
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
        if _submitted_question_count(page, text) > submitted_question_baseline + 1:
            raise RuntimeError("GLM page submitted the message more than once")
        count = _response_count(page)
        current = _last_text(page) if count else ""
        if (
            count <= response_baseline
            and current == baseline_text
            and _rate_limit_visible(page)
        ):
            if _click_rate_limit_retry(page):
                sent_at = time.time()
                last = ""
                stable = 0
                appeared = False
                continue
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
    if not attempt.confirmed:
        if attempt.method == "click" and attempt.action_error is not None:
            controls.reject_control(PROVIDER_ID, controls.CONTROL_SEND_BUTTON)
        raise SubmissionUncertain("GLM submission status is uncertain")
    raise ResponseMissing(f"GLM response timed out after {response_timeout:.0f}s")


@controls.revival_send(PROVIDER_ID)
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
