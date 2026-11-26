"""Stable control fingerprints, selector candidates, and host validation."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse

from codey.providers.profiles import get_profile

CONTROL_MESSAGE_BOX = "message_box"
CONTROL_SEND_BUTTON = "send_button"
CONTROL_RESPONSE = "response"
CONTROL_LABELS = {
    CONTROL_MESSAGE_BOX: "message box",
    CONTROL_SEND_BUTTON: "send button",
    CONTROL_RESPONSE: "answer",
}


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
    raw_data = fingerprint.get("data")
    data = raw_data if isinstance(raw_data, dict) else dict[str, object]()
    candidates: list[str] = []
    for attr in ("data-testid", "data-test-id", "data-track-id", "data-track-name", "data-qa", "data-role"):
        value = _clean(data.get(attr), 120)
        if value:
            candidates.append(f'{tag}[{attr}={_css_str(value)}]')
            candidates.append(f'[{attr}={_css_str(value)}]')

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

    raw_classes = fingerprint.get("classes")
    classes = [cls for cls in raw_classes if _stable_token(cls)] if isinstance(raw_classes, list) else list[str]()
    if classes:
        parts = "".join(f'[class~={_css_str(cls)}]' for cls in classes[:3])
        candidates.append(f"{tag}{parts}")

    text = _clean(fingerprint.get("text"), 48)
    if text and action == CONTROL_SEND_BUTTON:
        candidates.append(f'{tag}:has-text({_css_str(text)})')
        candidates.append(f'[role="button"]:has-text({_css_str(text)})')
        candidates.append(f'button:has-text({_css_str(text)})')

    return _dedupe(candidates)


def control_fingerprint_is_valid(fingerprint: dict[str, Any], action: str) -> bool:
    if action == CONTROL_MESSAGE_BOX:
        return _looks_like_message_box(fingerprint)
    if action == CONTROL_SEND_BUTTON:
        return _looks_like_send_button(fingerprint) and not _looks_like_upload(fingerprint)
    if action == CONTROL_RESPONSE:
        return _looks_like_response(fingerprint)
    return True


def control_label(fingerprint: dict[str, Any]) -> str:
    for key in ("aria_label", "title", "placeholder", "text", "role", "tag"):
        value = _clean(fingerprint.get(key), 80)
        if value:
            return value
    return ""


def _page_host(page: Any) -> str:
    try:
        return (urlparse(str(page.url or "")).hostname or "").lower()
    except Exception:
        return ""


def _host_matches(current: str, saved: str) -> bool:
    if not saved or not current:
        return False
    current = (current or "").lower()
    saved = (saved or "").lower()
    return current == saved or current.endswith("." + saved)


def _provider_host_allowed(page: Any, provider_id: str) -> bool:
    current = _page_host(page)
    try:
        hosts = get_profile(provider_id).hosts
    except KeyError:
        return False
    return bool(current) and any(_host_matches(current, host) for host in hosts)


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
        if not attr.startswith("data-") or attr.startswith("data-session-"):
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
    raw_data = fingerprint.get("data")
    data = raw_data if isinstance(raw_data, dict) else dict[str, object]()
    classes = fingerprint.get("classes")
    if not isinstance(classes, list):
        classes = list[str]()
    parts = [
        fingerprint.get("tag"),
        fingerprint.get("role"),
        fingerprint.get("type"),
        fingerprint.get("aria_label"),
        fingerprint.get("title"),
        fingerprint.get("placeholder"),
        fingerprint.get("text"),
        " ".join(str(value) for value in classes),
        " ".join(str(value) for value in data.values()),
    ]
    return " ".join(_clean(part, 120).lower() for part in parts if part)


def _looks_like_message_box(fingerprint: dict[str, Any]) -> bool:
    tag = _clean(fingerprint.get("tag"), 32).lower()
    role = _clean(fingerprint.get("role"), 48).lower()
    input_type = _clean(fingerprint.get("type"), 32).lower()
    if tag == "input" and input_type in {"file", "hidden", "password"}:
        return False
    return tag in {"textarea", "input"} or role in {"textbox", "searchbox"} or bool(
        fingerprint.get("contenteditable") or fingerprint.get("placeholder")
    )


def _looks_like_response(fingerprint: dict[str, Any]) -> bool:
    tag = _clean(fingerprint.get("tag"), 32).lower()
    if tag in {"button", "textarea", "input", "nav", "header", "footer"}:
        return False
    return bool(
        fingerprint.get("text")
        or fingerprint.get("classes")
        or fingerprint.get("data")
    )


def _looks_like_send_button(fingerprint: dict[str, Any]) -> bool:
    tag = _clean(fingerprint.get("tag"), 32).lower()
    role = _clean(fingerprint.get("role"), 48).lower()
    if tag == "button" or role == "button":
        return True
    classes = {
        _clean(item, 64).lower()
        for item in fingerprint.get("classes", [])
        if isinstance(item, str)
    }
    if "enter" in classes:
        return True
    text = _combined_text(fingerprint)
    return any(word in text for word in ("send", "submit", "发送", "送出"))


def _looks_like_upload(fingerprint: dict[str, Any]) -> bool:
    text = _combined_text(fingerprint)
    return any(word in text for word in ("upload", "attach", "file", "上传", "附件"))
