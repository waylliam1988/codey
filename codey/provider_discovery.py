"""Bounded semantic discovery for provider controls and new answer regions."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any


MESSAGE_BOX = "message_box"
SEND_BUTTON = "send_button"


@dataclass(frozen=True)
class Discovery:
    locator: Any
    fingerprint: dict[str, Any]


def find_control(page: Any, action: str, *, anchor: Any | None = None) -> Discovery | None:
    if action not in {MESSAGE_BOX, SEND_BUTTON}:
        return None
    anchor_box = _bounding_box(anchor)
    token = f"codey_{time.time_ns()}"
    try:
        raw = page.evaluate(_DISCOVER_CONTROLS_JS, {"token": token, "anchorBox": anchor_box})
    except Exception:
        return None
    candidates = [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
    ranked = sorted(
        ((score_control_candidate(item, action, anchor_box), item) for item in candidates),
        key=lambda pair: pair[0],
        reverse=True,
    )
    threshold = 58 if action == MESSAGE_BOX else 62
    if not ranked or ranked[0][0] < threshold:
        return None
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 12:
        return None
    best = ranked[0][1]
    locator = _unique_visible(page, str(best.get("selector") or ""))
    fingerprint = best.get("fingerprint")
    if locator is None or not isinstance(fingerprint, dict):
        return None
    return Discovery(locator, fingerprint)


def score_control_candidate(
    candidate: dict[str, Any],
    action: str,
    anchor_box: dict[str, Any] | None = None,
) -> int:
    fingerprint = _fingerprint(candidate.get("fingerprint"))
    if not candidate.get("visible", True):
        return -1000
    text = _combined_text(fingerprint)
    tag = fingerprint["tag"]
    role = fingerprint["role"]
    score = 0
    if action == MESSAGE_BOX:
        if tag == "textarea":
            score += 55
        elif fingerprint["contenteditable"]:
            score += 48
        elif role == "textbox":
            score += 42
        elif tag == "input" and fingerprint["type"] in {"", "text"}:
            score += 30
        else:
            return -1000
        if any(word in text for word in ("message", "chat", "ask", "发送消息", "输入消息", "提问")):
            score += 22
        if any(word in text for word in ("search", "find", "password", "搜索", "查找", "密码")):
            score -= 80
        if float(candidate.get("bottom_ratio") or 0) >= 0.55:
            score += 14
        if float(candidate.get("area") or 0) >= 1200:
            score += 8
        return score

    if action != SEND_BUTTON or (tag != "button" and role != "button"):
        return -1000
    score += 28
    if any(word in text for word in ("send", "submit", "发送", "送出")):
        score += 48
    if "primary" in text:
        score += 12
    if "filled" in text:
        score += 8
    if "circle" in text:
        score += 4
    if any(word in text for word in ("upload", "attach", "delete", "remove", "stop", "regenerate", "上传", "附件", "删除", "停止", "重新生成")):
        score -= 120
    distance = candidate.get("anchor_distance")
    if anchor_box and isinstance(distance, (int, float)):
        score += 36 if distance <= 180 else 15 if distance <= 420 else -35
    elif not any(word in text for word in ("send", "submit", "发送", "送出")):
        score -= 30
    if candidate.get("enabled", True):
        score += 6
    if float(candidate.get("bottom_ratio") or 0) >= 0.55:
        score += 8
    return score


def start_response_watch(page: Any) -> str:
    token = f"codey_response_{time.time_ns()}"
    try:
        page.evaluate(_START_RESPONSE_WATCH_JS, {"token": token})
    except Exception:
        return ""
    return token


def stop_response_watch(page: Any, token: str) -> None:
    if not token:
        return
    try:
        page.evaluate(_STOP_RESPONSE_WATCH_JS, {"token": token})
    except Exception:
        pass


def find_response(page: Any, token: str) -> Discovery | None:
    if not token:
        return None
    try:
        raw = page.evaluate(_READ_RESPONSE_WATCH_JS, {"token": token})
    except Exception:
        return None
    candidates = [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
    ranked = sorted(
        ((score_response_candidate(item), item) for item in candidates),
        key=lambda pair: pair[0],
        reverse=True,
    )
    if not ranked or ranked[0][0] < 48:
        return None
    best = ranked[0][1]
    locator = _unique_visible(page, str(best.get("selector") or ""))
    fingerprint = best.get("fingerprint")
    if locator is None or not isinstance(fingerprint, dict):
        return None
    return Discovery(locator, fingerprint)


def score_response_candidate(candidate: dict[str, Any]) -> int:
    fingerprint = _fingerprint(candidate.get("fingerprint"))
    if not candidate.get("visible", True):
        return -1000
    tag = fingerprint["tag"]
    if tag in {"button", "textarea", "input", "nav", "header", "footer"}:
        return -1000
    text = _clean(candidate.get("text"), 4000)
    if not text:
        return -1000
    hints = _combined_text(fingerprint)
    score = 8 + min(18, len(text) // 40)
    if "assistant" in hints:
        score += 55
    if "response" in hints or "answer" in hints:
        score += 38
    if "markdown" in hints or "prose" in hints:
        score += 40
    if tag in {"article", "pre"}:
        score += 18
    if text.lstrip().startswith(("{", "```")):
        score += 14
    if any(word in hints for word in ("user", "prompt", "sidebar", "navigation", "composer")):
        score -= 55
    if float(candidate.get("bottom_ratio") or 0) >= 0.25:
        score += 8
    return score


def _unique_visible(page: Any, selector: str) -> Any | None:
    if not selector:
        return None
    locator = page.locator(selector)
    matches = []
    try:
        count = int(locator.count())
    except Exception:
        return None
    for index in range(count):
        candidate = locator.nth(index)
        try:
            if candidate.is_visible():
                matches.append(candidate)
        except Exception:
            continue
    return matches[0] if len(matches) == 1 else None


def _bounding_box(locator: Any | None) -> dict[str, Any] | None:
    if locator is None:
        return None
    try:
        return locator.bounding_box()
    except Exception:
        return None


def _fingerprint(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    return {
        "tag": _clean(raw.get("tag"), 32).lower(),
        "role": _clean(raw.get("role"), 48).lower(),
        "type": _clean(raw.get("type"), 32).lower(),
        "aria_label": _clean(raw.get("ariaLabel") or raw.get("aria_label"), 80),
        "title": _clean(raw.get("title"), 80),
        "placeholder": _clean(raw.get("placeholder"), 120),
        "text": _clean(raw.get("text"), 80),
        "contenteditable": bool(raw.get("contentEditable") or raw.get("contenteditable")),
        "classes": raw.get("classes") if isinstance(raw.get("classes"), list) else [],
        "data": raw.get("data") if isinstance(raw.get("data"), dict) else {},
    }


def _combined_text(fingerprint: dict[str, Any]) -> str:
    data = fingerprint["data"]
    parts = [
        fingerprint["tag"], fingerprint["role"], fingerprint["type"],
        fingerprint["aria_label"], fingerprint["title"], fingerprint["placeholder"],
        fingerprint["text"], " ".join(fingerprint["classes"]), " ".join(data.values()),
    ]
    return " ".join(_clean(part, 120).lower() for part in parts if part)


def _clean(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


_DISCOVER_CONTROLS_JS = r"""
({ token, anchorBox }) => {
  const selector = 'button,[role="button"],textarea,input,[role="textbox"],[contenteditable="true"],[contenteditable="plaintext-only"]';
  const viewportHeight = Math.max(1, window.innerHeight || document.documentElement.clientHeight || 1);
  const items = []; let index = 0;
  for (const el of document.querySelectorAll(selector)) {
    const style = window.getComputedStyle(el); const box = el.getBoundingClientRect();
    const visible = style.visibility !== 'hidden' && style.display !== 'none' && box.width > 1 && box.height > 1;
    if (!visible) continue;
    const marker = el.getAttribute('data-codey-auto-candidate') || (token + '_' + index++);
    if (!el.hasAttribute('data-codey-auto-candidate')) el.setAttribute('data-codey-auto-candidate', marker);
    const data = {};
    for (const attr of Array.from(el.attributes || [])) if (attr.name && attr.name.startsWith('data-') && !attr.name.startsWith('data-codey-')) data[attr.name] = attr.value || '';
    let distance = null;
    if (anchorBox) {
      const ax = Number(anchorBox.x || 0) + Number(anchorBox.width || 0) / 2;
      const ay = Number(anchorBox.y || 0) + Number(anchorBox.height || 0) / 2;
      distance = Math.hypot(ax - (box.x + box.width / 2), ay - (box.y + box.height / 2));
    }
    items.push({ selector: '[data-codey-auto-candidate="' + marker + '"]', visible,
      enabled: !el.disabled && el.getAttribute('aria-disabled') !== 'true', area: box.width * box.height,
      bottom_ratio: Math.max(0, Math.min(1, (box.y + box.height / 2) / viewportHeight)), anchor_distance: distance,
      fingerprint: { tag: (el.tagName || '').toLowerCase(), role: el.getAttribute('role') || '', type: el.getAttribute('type') || '',
        ariaLabel: el.getAttribute('aria-label') || '', title: el.getAttribute('title') || '', placeholder: el.getAttribute('placeholder') || '',
        text: (el.innerText || el.textContent || '').trim(), contentEditable: !!el.isContentEditable,
        classes: Array.from(el.classList || []), data } });
  }
  return items;
}
"""


_START_RESPONSE_WATCH_JS = r"""
({ token }) => {
  const old = window.__codeyResponseWatch; if (old && old.observer) old.observer.disconnect();
  const state = { token, changed: [], seen: new WeakSet(), observer: null, nextMarker: 0 };
  const remember = (raw) => {
    let el = raw && raw.nodeType === 3 ? raw.parentElement : raw;
    for (let depth = 0; el && depth < 5; depth++, el = el.parentElement) {
      if (el === document.body || el === document.documentElement) break;
      if (!state.seen.has(el)) { state.seen.add(el); state.changed.push(el); }
    }
  };
  state.observer = new MutationObserver((mutations) => {
    for (const mutation of mutations) { remember(mutation.target); for (const node of mutation.addedNodes || []) remember(node); }
  });
  state.observer.observe(document.body, { childList: true, subtree: true, characterData: true });
  window.__codeyResponseWatch = state;
}
"""


_READ_RESPONSE_WATCH_JS = r"""
({ token }) => {
  const state = window.__codeyResponseWatch; if (!state || state.token !== token) return [];
  const viewportHeight = Math.max(1, window.innerHeight || document.documentElement.clientHeight || 1);
  const items = [];
  for (const el of state.changed) {
    if (!el || !el.isConnected || !el.getBoundingClientRect) continue;
    const style = window.getComputedStyle(el); const box = el.getBoundingClientRect();
    const visible = style.visibility !== 'hidden' && style.display !== 'none' && box.width > 1 && box.height > 1;
    const text = (el.innerText || el.textContent || '').trim();
    if (!visible || !text || text.length > 250000) continue;
    const marker = el.getAttribute('data-codey-response-candidate') || (token + '_' + state.nextMarker++);
    if (!el.hasAttribute('data-codey-response-candidate')) el.setAttribute('data-codey-response-candidate', marker);
    const data = {};
    for (const attr of Array.from(el.attributes || [])) if (attr.name && attr.name.startsWith('data-') && !attr.name.startsWith('data-codey-')) data[attr.name] = attr.value || '';
    items.push({ selector: '[data-codey-response-candidate="' + marker + '"]', visible, text,
      bottom_ratio: Math.max(0, Math.min(1, (box.y + box.height / 2) / viewportHeight)),
      fingerprint: { tag: (el.tagName || '').toLowerCase(), role: el.getAttribute('role') || '', type: el.getAttribute('type') || '',
        ariaLabel: el.getAttribute('aria-label') || '', title: el.getAttribute('title') || '', placeholder: el.getAttribute('placeholder') || '',
        text: '', contentEditable: !!el.isContentEditable, classes: Array.from(el.classList || []), data } });
  }
  return items;
}
"""


_STOP_RESPONSE_WATCH_JS = r"""
({ token }) => {
  const state = window.__codeyResponseWatch;
  if (state && state.token === token) { if (state.observer) state.observer.disconnect(); window.__codeyResponseWatch = null; }
  for (const el of document.querySelectorAll('[data-codey-response-candidate]')) el.removeAttribute('data-codey-response-candidate');
  for (const el of document.querySelectorAll('[data-codey-auto-candidate]')) el.removeAttribute('data-codey-auto-candidate');
}
"""
