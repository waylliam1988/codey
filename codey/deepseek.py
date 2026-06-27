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

from codey.web_clipboard import copy_action_text

INPUT = "textarea.ds-scroll-area"
SEND_READY = 'div[role="button"].ds-button--primary:not(.ds-button--disabled)'
RESPONSE = ".ds-markdown"
RESPONSE_ACTION = '[role="button"]'

DEEPSEEK_URL = "https://chat.deepseek.com/"
READY_TIMEOUT = 90.0
TIMEOUT_GRACE = 90.0
COPY_READY_TIMEOUT = 10.0


def new_chat(page) -> None:
    """Reset the page to a fresh DeepSeek conversation (clears prior context)."""
    page.goto(DEEPSEEK_URL, wait_until="domcontentloaded", timeout=60000)
    wait_ready(page)


def wait_ready(page: Page, timeout: float = READY_TIMEOUT) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if page.locator(INPUT).first.is_visible():
                return
        except Exception:
            pass
        time.sleep(0.4)
    raise TimeoutError("DeepSeek chat input did not appear. Are you logged in?")


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
    return page.evaluate(
        """(extractJs) => {
            const list = document.querySelectorAll('.ds-markdown');
            if (!list.length) return '';
            const fn = eval('(' + extractJs + ')');
            return fn(list[list.length-1]);
        }""",
        _EXTRACT_JS,
    )


def _response_count(page: Page) -> int:
    return page.locator(RESPONSE).count()


def _copy_last_text(page: Page) -> str:
    """Read the source response through DeepSeek's first answer action."""
    responses = page.locator(RESPONSE)
    if not responses.count():
        return ""
    actions = responses.last.locator("xpath=../..").locator(RESPONSE_ACTION)
    deadline = time.time() + COPY_READY_TIMEOUT
    while time.time() < deadline:
        if actions.count():
            copy_button = actions.first
            try:
                if copy_button.is_visible():
                    return copy_action_text(page, copy_button, origin=DEEPSEEK_URL)
            except Exception:
                pass
        time.sleep(0.2)
    return ""


def _final_text(page: Page) -> str:
    raw = _copy_last_text(page)
    if not raw:
        raise RuntimeError("Could not read the raw DeepSeek response")
    return raw


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
        except Exception:
            pass
        time.sleep(tick)
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
    wait_ready(page)

    baseline = _response_count(page)
    baseline_text = _last_text(page) if baseline else ""

    ta = page.locator(INPUT).first
    ta.click()
    ta.fill(text)
    time.sleep(0.3)

    deadline = time.time() + 5
    btn = None
    while time.time() < deadline:
        loc = page.locator(SEND_READY)
        if loc.count() > 0:
            btn = loc.last
            break
        time.sleep(0.2)
    if btn is None:
        ta.press("Enter")
    else:
        btn.click()

    sent_at = time.time()
    start_deadline = sent_at + response_timeout
    last = ""
    stable = 0
    appeared = False
    while time.time() < start_deadline:
        time.sleep(tick)
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
    raise TimeoutError(f"DeepSeek response timed out after {response_timeout:.0f}s")
