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

import time

from playwright.sync_api import Page

from codey import cancellation, provider_controls as controls
from codey.provider_profiles import get_profile
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


def new_chat(page) -> None:
    """Reset the page to a fresh DeepSeek conversation (clears prior context)."""
    cancellation.check()
    page.goto(DEEPSEEK_URL, wait_until="domcontentloaded", timeout=60000)
    wait_ready(page)


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
            except cancellation.TaskCancelled:
                raise
            except Exception:
                pass
        cancellation.wait(0.2)
    return ""


def _final_text(page: Page) -> str:
    raw = _copy_last_text(page)
    if not raw:
        raw = _last_text(page)
    if not raw:
        raise RuntimeError("Could not read the DeepSeek response")
    controls.confirm_control(PROVIDER_ID, controls.CONTROL_RESPONSE)
    return raw


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
        return not controls.control_has_text(message_box, submitted_text)
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
    last = ""
    while time.time() < deadline:
        try:
            current = _last_text(page) if _response_count(page) else ""
            if current and (_response_count(page) > baseline or current != baseline_text):
                last = current
                try:
                    return _final_text(page)
                except RuntimeError:
                    pass
        except cancellation.TaskCancelled:
            raise
        except Exception:
            pass
        cancellation.wait(tick)
    return ""


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

    controls.start_response_watch(page, PROVIDER_ID)
    try:
        ta = _message_box(page, teach=True)
        if ta is None:
            raise TimeoutError("DeepSeek chat input is not visible")
        cancellation.check()
        ta.click()
        cancellation.check()
        ta.fill(text)
        if controls.control_has_text(ta, text):
            controls.confirm_control(PROVIDER_ID, controls.CONTROL_MESSAGE_BOX)
        cancellation.wait(0.3)

        btn = _send_button(page, timeout=5)
        if btn is not None:
            cancellation.check()
            btn.click()
        else:
            cancellation.check()
            ta.press("Enter")
        submitted = _wait_submission_started(page, baseline, baseline_text, text)
        if submitted and btn is not None:
            controls.confirm_control(PROVIDER_ID, controls.CONTROL_SEND_BUTTON)
        elif btn is not None:
            controls.reject_control(PROVIDER_ID, controls.CONTROL_SEND_BUTTON)
        if not submitted and controls.control_has_text(ta, text):
            cancellation.check()
            ta.press("Enter")
            submitted = _wait_submission_started(page, baseline, baseline_text, text)
        if not submitted and controls.can_teach():
            btn = _send_button(page, teach=True)
            if btn is not None:
                cancellation.check()
                btn.click()
                submitted = _wait_submission_started(page, baseline, baseline_text, text)
                if submitted:
                    controls.confirm_control(PROVIDER_ID, controls.CONTROL_SEND_BUTTON)
                else:
                    controls.reject_control(PROVIDER_ID, controls.CONTROL_SEND_BUTTON)
        if not submitted:
            raise TimeoutError("DeepSeek did not submit the message")

        sent_at = time.time()
        start_deadline = sent_at + response_timeout
        last = ""
        stable = 0
        appeared = False
        while time.time() < start_deadline:
            cancellation.wait(tick)
            current = _last_text(page)
            if _response_count(page) <= baseline and current == baseline_text:
                continue
            appeared = True
            if not current:
                stable = 0
                continue
            if current == last and (time.time() - sent_at) >= min_wait:
                stable += 1
                if stable >= stable_ticks:
                    return _final_text(page)
            else:
                stable = 0
                last = current
        late = _wait_late_response(page, baseline, baseline_text=baseline_text, grace=TIMEOUT_GRACE, tick=tick)
        if late:
            return late
        if appeared and last:
            return _final_text(page)
        recovered = controls.recover_response(page, PROVIDER_ID, lambda: _final_text(page))
        if recovered is not None:
            return recovered
        controls.reject_control(PROVIDER_ID, controls.CONTROL_RESPONSE)
        raise TimeoutError(f"DeepSeek response timed out after {response_timeout:.0f}s")
    finally:
        controls.stop_response_watch(page, PROVIDER_ID)
