"""DeepSeek web driver, based on selectors verified against the live page.

Selectors discovered via probe.py / probe2.py:
    input         textarea.ds-scroll-area
    send button   div[role="button"].ds-button--primary
                    (disabled state adds .ds-button--disabled)
    assistant msg .ds-markdown.ds-assistant-message-main-content

DeepSeek's UI exposes no aria-labels and no obvious "stop generating" button,
so we detect completion by polling: a new .ds-markdown element appears, its
text stops growing for N consecutive ticks, and at least min_wait seconds have
elapsed since the send.
"""

from __future__ import annotations

import json
import time

from playwright.sync_api import Page

from codey import (
    cancellation,
    provider_controls as controls,
    provider_flow,
    provider_send_loop as send_loop,
)
from codey.provider_profiles import get_profile
from codey.provider_diagnostics import ControlMissing, RateLimited, ResponseMissing
from codey.provider_timeouts import navigation_timeout_ms, remaining, start_deadline
from codey.provider_submission import (
    SendAttempt,
    SubmissionUncertain,
    confirm_submission,
)
from codey.web_clipboard import copy_action_text

PROVIDER_ID = "deepseek"
PROFILE = get_profile(PROVIDER_ID)
INPUT = PROFILE.selector("message_box")
SEND_READY = PROFILE.selector("send_button")
RESPONSE = PROFILE.combined("response")
RESPONSE_ACTION = PROFILE.combined("response_action")

DEEPSEEK_URL = "https://chat.deepseek.com/"
READY_TIMEOUT = 90.0
TIMEOUT_GRACE = 90.0
COPY_READY_TIMEOUT = 10.0
SUBMIT_CONFIRM_TIMEOUT = 15.0
RATE_LIMIT_COOLDOWN = 10.0
RATE_LIMIT_TEXT = "消息发送过于频繁"
RATE_LIMIT_RETRY_BUTTON = "div[role='button'].ds-button--warning"
RATE_LIMIT_RETRY_TEXT = "重试"
JSON_TOOL_STABLE_TICKS = 2


def new_chat(page, timeout: float | None = None) -> None:
    """Reset the page to a fresh DeepSeek conversation (clears prior context)."""
    cancellation.check()
    deadline = start_deadline(timeout)
    page.goto(
        DEEPSEEK_URL,
        wait_until="domcontentloaded",
        timeout=navigation_timeout_ms(deadline),
    )
    if deadline is None:
        wait_ready(page)
    else:
        wait_ready(page, timeout=remaining(deadline, READY_TIMEOUT))


def wait_ready(page: Page, timeout: float = READY_TIMEOUT) -> None:
    cancellation.check()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _message_box(page) is not None:
            return
        cancellation.wait(0.4)
    if _message_box(page, teach=True) is not None:
        return
    raise TimeoutError("DeepSeek chat input did not appear. Are you logged in?")


def _message_box(page: Page, *, teach: bool = False):
    return controls.locate_control(
        page,
        PROVIDER_ID,
        controls.CONTROL_MESSAGE_BOX,
        PROFILE.selectors("message_box"),
        teach=teach,
    )


def _send_button(page: Page, *, timeout: float = 0.0, teach: bool = False):
    message_box = _message_box(page)
    return controls.locate_control(
        page,
        PROVIDER_ID,
        controls.CONTROL_SEND_BUTTON,
        PROFILE.selectors("send_button"),
        timeout=timeout,
        require_enabled=True,
        teach=teach,
        anchor=message_box,
    )


# Reconstruct fenced markdown from the rendered DOM.  Two important quirks:
#   1. DeepSeek wraps each code block in <div class="md-code-block">…</div>
#      with a sibling banner (.md-code-block-banner) that contains the
#      language label as plain text plus toolbar buttons ("复制", "下载",
#      "Run", etc.).  Naive innerText would inline that banner text right
#      before the code, garbling the output.  We skip any element whose
#      class contains "md-code-block-banner".
#   2. The <pre> has no inner <code> and no language class; the banner span
#      is the only place the language label lives.  We pull it from there.
_EXTRACT_JS = r"""
(el) => {
  const chunks = [];
  function bannerLang(preParent) {
    if (!preParent) return '';
    const banner = preParent.querySelector('[class*="md-code-block-banner"]');
    if (!banner) return '';
    const span = banner.querySelector('span');
    const txt = (span ? span.innerText : banner.innerText || '').trim();
    return txt.split(/\s+/)[0] || '';
  }
  function visit(node) {
    if (node.nodeType === 3) {
      chunks.push(node.textContent);
      return;
    }
    if (node.nodeType !== 1) return;
    const cls = (node.className || '') + '';
    if (cls.includes('md-code-block-banner')) return;
    const tag = node.tagName.toLowerCase();
    if (tag === 'pre') {
      const lang = bannerLang(node.parentElement);
      const text = (node.innerText || '').replace(/\n$/, '');
      chunks.push('\n```' + lang + '\n' + text + '\n```\n');
      return;
    }
    if (tag === 'code' && node.parentElement
        && node.parentElement.tagName.toLowerCase() !== 'pre') {
      chunks.push('`' + (node.innerText || '') + '`');
      return;
    }
    if (tag === 'br') { chunks.push('\n'); return; }
    if (tag === 'p' || tag === 'li') {
      for (const c of node.childNodes) visit(c);
      chunks.push('\n');
      return;
    }
    for (const c of node.childNodes) visit(c);
  }
  visit(el);
  return chunks.join('').replace(/\n{3,}/g, '\n\n').trim();
}
"""


def _last_text(page: Page) -> str:
    response = controls.locate_response(page, PROVIDER_ID, PROFILE.selectors("response"))
    if response is None:
        return ""
    return response.evaluate(_EXTRACT_JS)


def _response_count(page: Page) -> int:
    return controls.response_count(page, PROVIDER_ID, PROFILE.selectors("response"))


def _copy_last_text(page: Page) -> str:
    """Read the source response through DeepSeek's first answer action."""
    response = controls.locate_response(page, PROVIDER_ID, PROFILE.selectors("response"))
    if response is None:
        return ""
    actions = response.locator("xpath=../..").locator(RESPONSE_ACTION)
    deadline = time.time() + COPY_READY_TIMEOUT
    while time.time() < deadline:
        if actions.count():
            copy_button = actions.first
            try:
                if copy_button.is_visible():
                    cancellation.check()
                    return copy_action_text(page, copy_button, origin=DEEPSEEK_URL)
            except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
                raise
            except Exception:
                pass
        cancellation.wait(0.2)
    return ""


def _final_text(page: Page) -> str:
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
        raise RuntimeError("Could not read the DeepSeek response")
    controls.confirm_control(PROVIDER_ID, controls.CONTROL_RESPONSE)
    return raw


def _is_json_tool_reply(text: str) -> bool:
    try:
        value = json.loads(str(text or "").strip())
    except (TypeError, ValueError):
        return False
    return isinstance(value, dict) and bool(value.get("tool") or value.get("name"))


def _looks_like_json_tool_reply(text: str) -> bool:
    clean = str(text or "").lstrip()
    return clean.startswith("{") and ('"tool"' in clean or '"name"' in clean)


def _normalize_final_json_tool_reply(text: str) -> str:
    repaired = _repair_missing_trailing_braces_json_tool_reply(text)
    return repaired or text


def _repair_missing_trailing_braces_json_tool_reply(text: str) -> str:
    clean = str(text or "").strip()
    if not clean or not _looks_like_json_tool_reply(clean):
        return ""
    if _is_json_tool_reply(clean):
        return clean

    in_string = False
    escaped = False
    depth = 0
    started = False
    for char in clean:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == "{":
            started = True
            depth += 1
            continue
        if char == "}":
            if depth <= 0:
                return ""
            depth -= 1
            continue
        if started and depth == 0 and char.strip():
            return ""

    if in_string or depth <= 0 or depth > 2:
        return ""
    candidate = clean + ("}" * depth)
    return candidate if _is_json_tool_reply(candidate) else ""


def _submission_started(
    page: Page,
    baseline: int,
    baseline_text: str,
    submitted_text: str,
) -> bool:
    try:
        count = _response_count(page)
        current = _last_text(page) if count else ""
    except Exception:
        return False
    if count > baseline or (current and current != baseline_text):
        return True
    message_box = _message_box(page)
    if message_box is None:
        return False
    try:
        input_empty = not controls.control_has_text(message_box, submitted_text)
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


def _wait_submission_started(
    page: Page,
    baseline: int,
    baseline_text: str,
    submitted_text: str,
) -> bool:
    deadline = time.time() + SUBMIT_CONFIRM_TIMEOUT
    while time.time() < deadline:
        if _submission_started(page, baseline, baseline_text, submitted_text):
            return True
        cancellation.wait(0.2)
    return False


def _rate_limit_visible(page: Page) -> bool:
    try:
        return RATE_LIMIT_TEXT in str(page.locator("body").inner_text(timeout=1000))
    except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
        raise
    except Exception:
        return False


def _click_rate_limit_retry(page: Page) -> bool:
    cancellation.wait(RATE_LIMIT_COOLDOWN)
    buttons = page.locator(RATE_LIMIT_RETRY_BUTTON)
    try:
        count = buttons.count()
        for index in range(count - 1, -1, -1):
            candidate = buttons.nth(index)
            if (
                candidate.is_visible()
                and RATE_LIMIT_RETRY_TEXT in candidate.inner_text(timeout=500)
            ):
                cancellation.check()
                candidate.click()
                return True
    except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
        raise
    except Exception:
        return False
    return False


def _submit(
    page: Page,
    message_box,
    baseline: int,
    baseline_text: str,
    submitted_text: str,
) -> SendAttempt:
    button = _send_button(page, timeout=5, teach=True)
    attempt = SendAttempt()
    if button is not None:
        attempt.submit("click", button.click)
    else:
        controls.reject_control(
            PROVIDER_ID, controls.CONTROL_SEND_BUTTON, page=page
        )
        attempt.submit("enter", lambda: message_box.press("Enter"))

    if _wait_submission_started(page, baseline, baseline_text, submitted_text):
        confirm_submission(attempt, PROVIDER_ID)
    return attempt


def _wait_late_response(
    page: Page,
    baseline: int,
    baseline_text: str = "",
    grace: float = TIMEOUT_GRACE,
    tick: float = 0.8,
) -> str:
    """Give DeepSeek a short final grace window after the main timeout.

    The site can create the assistant message right around our timeout
    boundary.  Without this last read, Codey may report a timeout even though
    a usable response has already appeared in the page.
    """
    deadline = time.time() + max(0.0, grace)
    while time.time() < deadline:
        try:
            current = _last_text(page) if _response_count(page) else ""
            if current and (_response_count(page) > baseline or current != baseline_text):
                try:
                    return _final_text(page)
                except RuntimeError:
                    pass
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
    """Send `text`, wait for the next assistant message to stabilise, return it."""
    cancellation.check()
    wait_ready(page)

    baseline = _response_count(page)
    baseline_text = _last_text(page) if baseline else ""

    with send_loop.response_watch(page, PROVIDER_ID):
        ta = _message_box(page, teach=True)
        if ta is None:
            controls.reject_control(
                PROVIDER_ID, controls.CONTROL_MESSAGE_BOX, page=page
            )
            raise ControlMissing("DeepSeek chat input is not visible")
        try:
            cancellation.check()
            ta.click()
            cancellation.check()
            ta.fill(text)
        except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
            raise
        except Exception:
            controls.reject_control(PROVIDER_ID, controls.CONTROL_MESSAGE_BOX)
            raise
        if not controls.control_has_text(ta, text):
            controls.reject_control(PROVIDER_ID, controls.CONTROL_MESSAGE_BOX)
            raise ControlMissing("DeepSeek chat input did not accept the complete message")
        controls.confirm_control(PROVIDER_ID, controls.CONTROL_MESSAGE_BOX)
        cancellation.wait(0.3)

        attempt = _submit(page, ta, baseline, baseline_text, text)

        ctx = send_loop.ProviderSendContext(
            page=page,
            provider_id=PROVIDER_ID,
            display_name="DeepSeek",
            sent_at=time.time(),
        )
        start_deadline = ctx.sent_at + response_timeout
        stable = 0
        while time.time() < start_deadline:
            cancellation.wait(tick)
            if (
                _response_count(page) <= baseline
                and _rate_limit_visible(page)
            ):
                if _click_rate_limit_retry(page):
                    ctx.reset_text_progress(sent_at=time.time())
                    stable = 0
                    continue
            current = _last_text(page)
            if _response_count(page) <= baseline and current == baseline_text:
                continue
            confirm_submission(attempt, PROVIDER_ID)
            ctx.appeared = True
            if not current:
                stable = 0
                continue
            same = ctx.same_as_last(current)
            observation = provider_flow.FlowObservation(
                response_stable=same,
                response_nonempty=bool(current),
            )
            ctx.trace.add(observation)
            if same and (time.time() - ctx.sent_at) >= min_wait:
                is_json_tool = _is_json_tool_reply(current)
                repairable_json_tool = False
                if _looks_like_json_tool_reply(current) and not is_json_tool:
                    repairable_json_tool = bool(
                        _repair_missing_trailing_braces_json_tool_reply(current)
                    )
                    if not repairable_json_tool:
                        continue
                stable += 1
                if stable >= JSON_TOOL_STABLE_TICKS and is_json_tool:
                    return send_loop.read_completion(
                        ctx,
                        lambda: _final_text(page),
                    )
                if repairable_json_tool and stable < stable_ticks:
                    continue
                if repairable_json_tool:
                    return send_loop.read_completion(
                        ctx,
                        lambda: _final_text(page),
                    )
                ready = send_loop.completion_ready(
                    ctx,
                    observation,
                    built_in_ready=stable >= stable_ticks,
                )
                if ready:
                    return send_loop.read_completion(
                        ctx,
                        lambda: _final_text(page),
                    )
            else:
                stable = 0
                ctx.last = current
        if _rate_limit_visible(page):
            raise RateLimited("DeepSeek is rate limited")
        late = _wait_late_response(page, baseline, baseline_text=baseline_text, grace=TIMEOUT_GRACE, tick=tick)
        if late:
            confirm_submission(attempt, PROVIDER_ID)
            return late
        if ctx.appeared and ctx.last:
            return _final_text(page)
        recovered = controls.recover_response(page, PROVIDER_ID, lambda: _final_text(page))
        if recovered is not None:
            confirm_submission(attempt, PROVIDER_ID)
            return recovered
        if not attempt.confirmed:
            if attempt.method == "click" and attempt.action_error is not None:
                controls.reject_control(PROVIDER_ID, controls.CONTROL_SEND_BUTTON)
            raise SubmissionUncertain("DeepSeek submission status is uncertain")
        raise ResponseMissing(f"DeepSeek response timed out after {response_timeout:.0f}s")
