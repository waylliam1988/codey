"""Small self-repair layer for provider page controls.

Provider adapters should keep their normal selectors first.  This module is a
fallback: when a site moves the message box or send button, Codey can ask the
user to click the control once and keep only that latest teaching.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

CONTROL_MESSAGE_BOX = "message_box"
CONTROL_SEND_BUTTON = "send_button"
CONTROL_LABELS = {
    CONTROL_MESSAGE_BOX: "message box",
    CONTROL_SEND_BUTTON: "send button",
}
CONTROL_STORE = Path.home() / ".codey" / "provider-controls.json"

_handler: Callable[["ControlTeachRequest"], Any] | None = None
_context = threading.local()


class ControlTeachCancelled(RuntimeError):
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


def set_teach_handler(handler: Callable[[ControlTeachRequest], Any] | None) -> None:
    global _handler
    _handler = handler


def can_teach() -> bool:
    return _handler is not None


def set_session_id(session_id: str) -> None:
    _context.session_id = session_id or ""


def visible_locator(page: Any, selector: str) -> Any | None:
    locator = page.locator(selector)
    try:
        count = locator.count()
    except Exception:
        return None
    try:
        count = int(count)
    except (TypeError, ValueError):
        return None
    for index in range(count - 1, -1, -1):
        candidate = locator.nth(index)
        try:
            if candidate.is_visible():
                return candidate
        except Exception:
            continue
    return None


def locate_control(
    page: Any,
    provider_id: str,
    action: str,
    selectors: list[str] | tuple[str, ...],
    *,
    timeout: float = 0.0,
    require_enabled: bool = False,
    teach: bool = False,
) -> Any | None:
    deadline = time.time() + max(0.0, timeout)
    first = True
    while first or time.time() < deadline:
        first = False
        for selector in selectors:
            control = visible_locator(page, selector)
            if _usable(control, require_enabled):
                return control
        control = saved_control(page, provider_id, action, require_enabled=require_enabled)
        if control is not None:
            return control
        if time.time() >= deadline:
            break
        time.sleep(0.2)
    if teach and can_teach():
        return request_teaching(
            page,
            provider_id,
            action,
            require_enabled=require_enabled,
        )
    return None


def saved_control(
    page: Any,
    provider_id: str,
    action: str,
    *,
    require_enabled: bool = False,
) -> Any | None:
    record = load_control(provider_id, action)
    if not record:
        return None
    if not _host_matches(_page_host(page), str(record.get("host") or "")):
        return None
    fingerprint = record.get("fingerprint")
    if not isinstance(fingerprint, dict):
        return None
    for selector in selector_candidates(fingerprint, action):
        try:
            control = visible_locator(page, selector)
        except Exception:
            continue
        if _usable(control, require_enabled):
            return control
    return None


def request_teaching(
    page: Any,
    provider_id: str,
    action: str,
    *,
    require_enabled: bool = False,
) -> Any:
    if _handler is None:
        label = CONTROL_LABELS.get(action, "control")
        raise TimeoutError(f"Could not find the {label} in the model page")
    request = ControlTeachRequest(
        provider_id=provider_id,
        action=action,
        page=page,
        session_id=str(getattr(_context, "session_id", "") or ""),
        require_enabled=require_enabled,
    )
    return _handler(request)


def start_click_capture(page: Any) -> str:
    token = "codey_" + str(int(time.time() * 1000))
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
                current_selector=f'[data-codey-teach-current="{token}"]',
            )
        label = control_label(fingerprint)
        raise ValueError(label or "clicked item was not usable")
    finally:
        _cleanup_capture(page)


def cancel_click_capture(page: Any) -> None:
    _cleanup_capture(page)


def resolve_captured_control(request: ControlTeachRequest, captured: CapturedControl) -> Any:
    save_control(request.provider_id, request.action, request.page, captured.fingerprint)
    control = visible_locator(request.page, captured.current_selector)
    if _usable(control, request.require_enabled):
        return control
    control = saved_control(
        request.page,
        request.provider_id,
        request.action,
        require_enabled=request.require_enabled,
    )
    if control is not None:
        return control
    label = CONTROL_LABELS.get(request.action, "control")
    raise TimeoutError(f"Could not reuse the taught {label}")


def load_controls(path: Path | None = None) -> dict[str, Any]:
    path = path or CONTROL_STORE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def save_control(
    provider_id: str,
    action: str,
    page: Any,
    fingerprint: dict[str, Any],
    *,
    path: Path | None = None,
) -> None:
    path = path or CONTROL_STORE
    data = load_controls(path)
    provider = data.setdefault(provider_id, {})
    if not isinstance(provider, dict):
        provider = {}
        data[provider_id] = provider
    provider[action] = {
        "host": _page_host(page),
        "fingerprint": fingerprint,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        raise OSError(f"Could not save the taught control: {path}") from exc


def load_control(provider_id: str, action: str, *, path: Path | None = None) -> dict[str, Any] | None:
    data = load_controls(path)
    provider = data.get(provider_id)
    if not isinstance(provider, dict):
        return None
    record = provider.get(action)
    return record if isinstance(record, dict) else None


def fingerprint_from_click(data: Any) -> dict[str, Any]:
    raw = data if isinstance(data, dict) else {}
    return {
        "tag": _clean(raw.get("tag"), 32).lower(),
        "role": _clean(raw.get("role"), 48).lower(),
        "type": _clean(raw.get("type"), 32).lower(),
        "aria_label": _clean(raw.get("ariaLabel"), 80),
        "title": _clean(raw.get("title"), 80),
        "placeholder": _clean(raw.get("placeholder"), 120),
        "text": _clean(raw.get("text"), 80),
        "contenteditable": bool(raw.get("contentEditable")),
        "classes": _stable_classes(raw.get("classes")),
        "data": _stable_data(raw.get("data")),
    }


def selector_candidates(fingerprint: dict[str, Any], action: str = "") -> list[str]:
    tag = _selector_tag(fingerprint.get("tag"))
    data = fingerprint.get("data") if isinstance(fingerprint.get("data"), dict) else {}
    candidates: list[str] = []
    for attr in ("data-testid", "data-test-id", "data-track-id", "data-track-name", "data-qa", "data-role"):
        value = _clean(data.get(attr), 120)
        if value:
            candidates.append(f'{tag}[{attr}={_css_str(value)}]')
            candidates.append(f'[{attr}={_css_str(value)}]')

    role = _clean(fingerprint.get("role"), 48)
    if role:
        candidates.append(f'{tag}[role={_css_str(role)}]')
        candidates.append(f'[role={_css_str(role)}]')

    for attr in ("aria_label", "title", "placeholder"):
        value = _clean(fingerprint.get(attr), 120)
        if not value:
            continue
        html_attr = attr.replace("_", "-")
        candidates.append(f'{tag}[{html_attr}={_css_str(value)}]')
        candidates.append(f'[{html_attr}={_css_str(value)}]')

    input_type = _clean(fingerprint.get("type"), 32)
    if input_type:
        candidates.append(f'{tag}[type={_css_str(input_type)}]')

    classes = [cls for cls in fingerprint.get("classes", []) if _stable_token(cls)]
    if classes:
        parts = "".join(f'[class~={_css_str(cls)}]' for cls in classes[:3])
        candidates.append(f"{tag}{parts}")

    text = _clean(fingerprint.get("text"), 48)
    if text and action == CONTROL_SEND_BUTTON:
        candidates.append(f'{tag}:has-text({_css_str(text)})')
        candidates.append(f'[role="button"]:has-text({_css_str(text)})')
        candidates.append(f'button:has-text({_css_str(text)})')

    if fingerprint.get("contenteditable"):
        candidates.append('[contenteditable="true"]')
        candidates.append('[contenteditable="plaintext-only"]')

    return _dedupe(candidates)


def control_fingerprint_is_valid(fingerprint: dict[str, Any], action: str) -> bool:
    if action == CONTROL_MESSAGE_BOX:
        return _looks_like_message_box(fingerprint)
    if action == CONTROL_SEND_BUTTON:
        return _looks_like_send_button(fingerprint) and not _looks_like_upload(fingerprint)
    return True


def control_label(fingerprint: dict[str, Any]) -> str:
    for key in ("aria_label", "title", "placeholder", "text", "role", "tag"):
        value = _clean(fingerprint.get(key), 80)
        if value:
            return value
    return ""


def _wait_for_click(page: Any, deadline: float) -> dict[str, Any]:
    while time.time() < deadline:
        captured = page.evaluate("window.__codeyTeachClick || null")
        if captured:
            return captured
        time.sleep(0.1)
    raise TimeoutError("Timed out waiting for a click in the model page")


def _cleanup_capture(page: Any) -> None:
    try:
        page.evaluate("window.__codeyTeachCleanup && window.__codeyTeachCleanup()")
    except Exception:
        pass


def _usable(control: Any | None, require_enabled: bool) -> bool:
    if control is None:
        return False
    if not require_enabled:
        return True
    try:
        return bool(control.is_enabled())
    except Exception:
        return False


def _page_host(page: Any) -> str:
    try:
        return urlparse(str(page.url or "")).netloc.lower()
    except Exception:
        return ""


def _host_matches(current: str, saved: str) -> bool:
    if not saved:
        return True
    return current == saved or current.endswith("." + saved) or saved.endswith("." + current)


def _clean(value: Any, limit: int = 120) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def _stable_classes(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [cls for cls in (_clean(item, 64) for item in value) if _stable_token(cls)][:8]


def _stable_data(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, str] = {}
    for key, item in value.items():
        attr = _clean(key, 48)
        if not attr.startswith("data-") or attr.startswith("data-codey-"):
            continue
        text = _clean(item, 120)
        if text:
            out[attr] = text
    return out


def _stable_token(value: str) -> bool:
    if not value or len(value) > 48:
        return False
    if re.fullmatch(r"[\da-f]{6,}", value, re.IGNORECASE):
        return False
    return bool(re.search(r"[a-zA-Z]", value))


def _selector_tag(value: Any) -> str:
    tag = _clean(value, 32).lower()
    return tag if re.fullmatch(r"[a-z][a-z0-9-]*", tag) else "*"


def _css_str(value: str) -> str:
    return json.dumps(value)


def _dedupe(items: list[str]) -> list[str]:
    out: list[str] = []
    for item in items:
        if item and item not in out:
            out.append(item)
    return out


def _combined_text(fingerprint: dict[str, Any]) -> str:
    data = fingerprint.get("data") if isinstance(fingerprint.get("data"), dict) else {}
    parts = [
        fingerprint.get("tag"),
        fingerprint.get("role"),
        fingerprint.get("type"),
        fingerprint.get("aria_label"),
        fingerprint.get("title"),
        fingerprint.get("placeholder"),
        fingerprint.get("text"),
        " ".join(fingerprint.get("classes", [])),
        " ".join(data.values()),
    ]
    return " ".join(_clean(part, 120).lower() for part in parts if part)


def _looks_like_message_box(fingerprint: dict[str, Any]) -> bool:
    tag = _clean(fingerprint.get("tag"), 32).lower()
    role = _clean(fingerprint.get("role"), 48).lower()
    return tag in {"textarea", "input"} or role in {"textbox", "searchbox"} or bool(
        fingerprint.get("contenteditable") or fingerprint.get("placeholder")
    )


def _looks_like_send_button(fingerprint: dict[str, Any]) -> bool:
    tag = _clean(fingerprint.get("tag"), 32).lower()
    role = _clean(fingerprint.get("role"), 48).lower()
    if tag == "button" or role == "button":
        return True
    text = _combined_text(fingerprint)
    return any(word in text for word in ("send", "submit", "发送", "送出"))


def _looks_like_upload(fingerprint: dict[str, Any]) -> bool:
    text = _combined_text(fingerprint)
    return any(word in text for word in ("upload", "attach", "file", "上传", "附件"))


_INSTALL_CAPTURE_JS = r"""
({ token }) => {
  if (window.__codeyTeachCleanup) window.__codeyTeachCleanup();
  window.__codeyTeachClick = null;
  const interactive = 'button,[role="button"],textarea,input,[contenteditable="true"],[contenteditable="plaintext-only"]';
  const handler = (event) => {
    const raw = event.target;
    const el = raw && raw.closest ? (raw.closest(interactive) || raw) : raw;
    if (!el || !el.getBoundingClientRect) return;
    event.preventDefault();
    event.stopPropagation();
    event.stopImmediatePropagation();
    el.setAttribute('data-codey-teach-current', token);
    const data = {};
    for (const attr of Array.from(el.attributes || [])) {
      if (attr.name && attr.name.startsWith('data-')) data[attr.name] = attr.value || '';
    }
    window.__codeyTeachClick = {
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
    window.__codeyTeachCleanup();
  };
  document.addEventListener('click', handler, true);
  window.__codeyTeachCleanup = () => {
    document.removeEventListener('click', handler, true);
    window.__codeyTeachCleanup = null;
  };
}
"""
