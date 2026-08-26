"""One-shot, model-assisted selection among bounded provider-page candidates."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable

from codey.runtime import cancellation
from codey.providers.discovery import Discovery


MAX_CANDIDATES = 8
MAX_LABEL = 64
DATA_KEYS = {
    "data-testid",
    "data-test-id",
    "data-track-id",
    "data-track-name",
    "data-qa",
    "data-role",
}
TAG_VALUES = frozenset({
    "article", "button", "div", "form", "input", "main", "p", "pre",
    "section", "span", "textarea",
})
ROLE_VALUES = frozenset({
    "article", "button", "feed", "group", "log", "main", "presentation",
    "region", "searchbox", "textbox",
})
INPUT_TYPE_VALUES = frozenset({
    "button", "checkbox", "color", "date", "datetime-local", "email", "file",
    "hidden", "image", "month", "number", "password", "radio", "range", "reset",
    "search", "submit", "tel", "text", "time", "url", "week",
})
SEMANTIC_TERMS = (
    "message", "chat", "ask", "prompt", "send", "submit", "search", "find",
    "password", "upload", "attach", "delete", "remove", "stop", "regenerate",
    "assistant", "response", "answer", "markdown", "prose", "输入消息", "提问",
    "发送", "送出", "搜索", "查找", "密码", "上传", "附件", "删除", "停止",
    "重新生成",
)
STRUCTURAL_TERMS = SEMANTIC_TERMS + (
    "button", "btn", "textbox", "textarea", "input", "editor", "composer",
    "primary", "filled", "circle", "icon", "contenteditable", "enter",
)


@dataclass(frozen=True)
class CandidateSummary:
    candidate_id: str
    tag: str
    role: str
    input_type: str
    label: str
    classes: tuple[str, ...]
    data: dict[str, str]
    enabled: bool | None
    proximity: str
    page_region: str
    size: str
    heuristic_score: int


@dataclass(frozen=True)
class ProfileDoctorRequest:
    provider_id: str
    action: str
    candidates: tuple[CandidateSummary, ...]
    page: Any = field(repr=False, compare=False)
    session_id: str = ""


def make_request(
    provider_id: str,
    action: str,
    page: Any,
    discoveries: Iterable[Discovery],
    *,
    session_id: str = "",
) -> ProfileDoctorRequest:
    summaries = tuple(
        _summarize(index, action, item)
        for index, item in enumerate(tuple(discoveries)[:MAX_CANDIDATES], start=1)
    )
    return ProfileDoctorRequest(provider_id, action, summaries, page, session_id)


def choose_candidate(
    request: ProfileDoctorRequest,
    send: Callable[[str], str],
) -> str | None:
    """Ask exactly once and accept only one known candidate id or null."""
    if not request.candidates:
        return None
    cancellation.check()
    reply = send(render_prompt(request))
    cancellation.check()
    payload = _decision_payload(reply)
    if payload is None:
        return None
    if not isinstance(payload, dict) or set(payload) != {"candidate_id"}:
        return None
    selected = payload.get("candidate_id")
    if selected is None:
        return None
    allowed = {item.candidate_id for item in request.candidates}
    return selected if isinstance(selected, str) and selected in allowed else None


def _decision_payload(reply: Any) -> dict[str, Any] | None:
    """Accept one unambiguous object even when provider chrome prefixes status text."""
    text = str(reply or "").strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        matches = re.findall(r"\{[^{}]{1,96}\}", text)
        if len(matches) != 1 or "```" in text:
            return None
        wrapper = text.replace(matches[0], "", 1).strip()
        if len(wrapper) > 160 or any(character in wrapper for character in "{}"):
            return None
        try:
            payload = json.loads(matches[0])
        except json.JSONDecodeError:
            return None
    return payload if isinstance(payload, dict) else None


def render_prompt(request: ProfileDoctorRequest) -> str:
    candidates = [asdict(item) for item in request.candidates]
    return (
        "You are selecting one web-page element for a local recovery check. "
        "The list is already bounded and contains no page content. "
        "Choose only when the semantic and structural evidence is sufficient. "
        "Do not invent selectors, code, coordinates, or another candidate. "
        "Reply with exactly one JSON object and no markdown: "
        '{"candidate_id":"c1"}. Reply {"candidate_id":null} when uncertain.\n'
        + json.dumps(
            {
                "target_provider": request.provider_id,
                "action": request.action,
                "candidates": candidates,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


def _summarize(index: int, action: str, item: Discovery) -> CandidateSummary:
    fingerprint = item.fingerprint if isinstance(item.fingerprint, dict) else {}
    tag = _safe_enum(fingerprint.get("tag"), TAG_VALUES)
    role = _safe_enum(fingerprint.get("role"), ROLE_VALUES)
    input_type = _safe_enum(fingerprint.get("type"), INPUT_TYPE_VALUES)
    label = ""
    if action != "response":
        for key in ("ariaLabel", "aria_label", "title", "placeholder", "text"):
            label = _semantic_label(fingerprint.get(key))
            if label:
                break
    classes = tuple(
        token
        for token in (_structure_hint(value) for value in fingerprint.get("classes", []))
        if token
    )[:6]
    raw_data = fingerprint.get("data") if isinstance(fingerprint.get("data"), dict) else {}
    data = {
        key: value
        for key, value in (
            (_token(raw_key, 32), _structure_hint(raw_value))
            for raw_key, raw_value in raw_data.items()
            if str(raw_key) in DATA_KEYS
        )
        if key and value
    }
    metadata = item.metadata if isinstance(item.metadata, dict) else {}
    return CandidateSummary(
        candidate_id=f"c{index}",
        tag=tag,
        role=role,
        input_type=input_type,
        label=label,
        classes=classes,
        data=data,
        enabled=_optional_bool(metadata.get("enabled")),
        proximity=_proximity(metadata.get("anchor_distance")),
        page_region=_page_region(metadata.get("bottom_ratio")),
        size=_size(metadata.get("area")),
        heuristic_score=int(item.score),
    )


def _safe_label(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()[:MAX_LABEL]
    if not text:
        return ""
    sensitive = (
        r"https?://|\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b|"
        r"\b[A-Za-z]:\\|(?:^|\s)/(?:Users|home|var|tmp)/|"
        r"\b(?:api[_ -]?key|password|bearer|secret|token)\b|"
        r"\bsk-[A-Za-z0-9_-]+\b|\b[A-Za-z0-9_=-]{24,}\b"
    )
    return "[redacted]" if re.search(sensitive, text, re.IGNORECASE) else text


def _semantic_label(value: Any) -> str:
    """Keep known control semantics, never arbitrary visible page text."""
    text = _safe_label(value)
    if not text or text == "[redacted]":
        return ""
    lowered = text.lower()
    return ",".join(dict.fromkeys(term for term in SEMANTIC_TERMS if term in lowered))


def _structure_hint(value: Any) -> str:
    text = _token(value, 80).lower()
    if not text:
        return ""
    return ",".join(dict.fromkeys(
        term
        for term in STRUCTURAL_TERMS
        if term in text and (term != "enter" or text == "enter")
    ))


def _token(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _safe_enum(value: Any, allowed: frozenset[str]) -> str:
    token = _token(value, 32).lower()
    if not token:
        return ""
    return token if token in allowed else "other"


def _optional_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _proximity(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "unknown"
    return "near" if value <= 180 else "medium" if value <= 420 else "far"


def _page_region(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "unknown"
    return "bottom" if value >= 0.66 else "middle" if value >= 0.33 else "top"


def _size(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "unknown"
    return "large" if value >= 12000 else "medium" if value >= 1200 else "small"