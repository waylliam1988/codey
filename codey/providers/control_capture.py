"""User click capture and immutable provider control teaching requests."""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass
from typing import Any, cast

from codey.providers.control_fingerprint import (
    CONTROL_LABELS,
    control_fingerprint_is_valid,
    control_label,
    fingerprint_from_click,
)
from codey.runtime.core import cancellation


class ControlTeachCancelled(cancellation.TaskCancelled):
    """Raised when the user stops a paused control teaching request."""


@dataclass(frozen=True)
class CapturedControl:
    fingerprint: dict[str, Any]
    current_selector: str


@dataclass(frozen=True)
class ControlTeachRequest:
    provider_id: str
    action: str
    page: Any
    session_id: str = ""
    require_enabled: bool = False

    @property
    def message(self) -> str:
        label = CONTROL_LABELS.get(self.action, "control")
        return f"Click the {label} in the model page"


def start_click_capture(page: Any) -> str:
    cancellation.check()
    token = "capture_" + str(int(time.time() * 1000))
    page.evaluate(_INSTALL_CAPTURE_JS, {"token": token})
    return token


def finish_click_capture(
    page: Any,
    token: str,
    action: str,
    *,
    timeout: float = 1.0,
) -> CapturedControl:
    deadline = time.time() + max(0.2, timeout)
    try:
        captured = _wait_for_click(page, deadline)
        fingerprint = fingerprint_from_click(captured)
        if control_fingerprint_is_valid(fingerprint, action):
            return CapturedControl(
                fingerprint=fingerprint,
                current_selector=f'[data-session-teach-current="{token}"]',
            )
        label = control_label(fingerprint)
        raise ValueError(label or "clicked item was not usable")
    finally:
        _cleanup_capture(page)


def cancel_click_capture(page: Any) -> None:
    _cleanup_capture(page)


def _wait_for_click(page: Any, deadline: float) -> dict[str, Any]:
    while time.time() < deadline:
        cancellation.check()
        captured = page.evaluate("window.__sessionTeachClick || null")
        if captured:
            return cast(dict[str, Any], captured)
        cancellation.wait(0.1)
    raise TimeoutError("Timed out waiting for a click in the model page")


def _cleanup_capture(page: Any) -> None:
    with contextlib.suppress(Exception):
        page.evaluate("window.__sessionTeachCleanup && window.__sessionTeachCleanup()")


_INSTALL_CAPTURE_JS = r"""
({ token }) => {
  if (window.__sessionTeachCleanup) window.__sessionTeachCleanup();
  window.__sessionTeachClick = null;
  const interactive = 'button,[role="button"],textarea,input,[contenteditable="true"],[contenteditable="plaintext-only"]';
  const handler = (event) => {
    const raw = event.target;
    const el = raw && raw.closest ? (raw.closest(interactive) || raw) : raw;
    if (!el || !el.getBoundingClientRect) return;
    event.preventDefault();
    event.stopPropagation();
    event.stopImmediatePropagation();
    el.setAttribute('data-session-teach-current', token);
    const data = {};
    for (const attr of Array.from(el.attributes || [])) {
      if (attr.name && attr.name.startsWith('data-')) data[attr.name] = attr.value || '';
    }
    window.__sessionTeachClick = {
      tag: (el.tagName || '').toLowerCase(),
      role: el.getAttribute('role') || '',
      type: el.getAttribute('type') || '',
      ariaLabel: el.getAttribute('aria-label') || '',
      title: el.getAttribute('title') || '',
      placeholder: el.getAttribute('placeholder') || '',
      text: (el.innerText || el.textContent || '').trim(),
      contentEditable: !!el.isContentEditable,
      classes: Array.from(el.classList || []),
      data,
    };
    window.__sessionTeachCleanup();
  };
  document.addEventListener('click', handler, true);
  window.__sessionTeachCleanup = () => {
    document.removeEventListener('click', handler, true);
    window.__sessionTeachCleanup = null;
  };
}
"""
