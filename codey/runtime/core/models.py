from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

TRUNCATED_RESULT_NOTICE = (
    "[truncated result: omitted content may contain relevant "
    "errors or code. Do not assume omitted content is clean. "
    "Use narrower offsets or rerun a narrower query/command if needed.]"
)
PROJECTION_WARNING_KEY = "_projection_warnings"
PROJECTION_MAX_DEPTH = 6
PROJECTION_MAX_ITEMS = 200
PROJECTION_MAX_KEY_CHARS = 80
PROJECTION_MAX_STRING_CHARS = 2_000
PROJECTION_MAX_WARNINGS = 8
MANAGED_OUTPUT_HANDLE_RE = re.compile(r"out_[A-Za-z0-9_.-]{1,80}")
MANAGED_OUTPUT_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def model_text_with_audit_markers(
    model_text: object,
    *,
    truncated: bool = False,
    audit: Mapping[str, object] | None = None,
) -> str:
    text = str(model_text or "")
    if truncated and TRUNCATED_RESULT_NOTICE not in text:
        text = f"{text}\n{TRUNCATED_RESULT_NOTICE}"
    managed = audit.get("managed_output") if isinstance(audit, Mapping) else None
    capture_truncated = bool(audit.get("capture_truncated")) if isinstance(audit, Mapping) else False
    footer = _managed_output_footer(managed, capture_truncated=capture_truncated)
    if footer and footer not in text:
        text = f"{text}\n{footer}"
    return text


def _managed_output_footer(value: object, *, capture_truncated: bool = False) -> str:
    managed = normalized_managed_output(value)
    if not managed:
        return ""
    # sha256 below is always the STORED artifact hash. A capture-truncated
    # run only saved an output receipt (head+tail), never the complete log,
    # so the framing must say receipt even when the store itself did not
    # truncate further. original_sha256 stays the hash of the stored text.
    receipt = bool(managed["stored_truncated"] or capture_truncated)
    framing = (
        "[output receipt retained locally: "
        if receipt
        else "[full output retained locally: "
    )
    footer = (
        f"{framing}"
        f"handle={managed['handle']}, "
        f"original_bytes={managed['original_bytes']}, "
        f"stored_bytes={managed['stored_bytes']}, "
        f"sha256={managed['sha256']}; "
    )
    if managed["stored_truncated"] and managed["original_sha256"]:
        footer += f"original_sha256={managed['original_sha256']}; "
    return footer + "handle is for local audit/export, not a tool.]"


def normalized_managed_output(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    handle = value.get("handle")
    if not isinstance(handle, str) or not MANAGED_OUTPUT_HANDLE_RE.fullmatch(handle):
        return {}
    return {
        "handle": handle,
        "original_bytes": _nonnegative_int(value.get("original_bytes")),
        "stored_bytes": _nonnegative_int(value.get("stored_bytes")),
        "sha256": _managed_output_sha256(value.get("sha256")),
        "original_sha256": _managed_output_sha256(value.get("original_sha256")),
        "stored_truncated": bool(value.get("stored_truncated")),
    }


def _managed_output_sha256(value: object) -> str:
    if not isinstance(value, str) or not MANAGED_OUTPUT_SHA256_RE.fullmatch(value):
        return ""
    return value


def _nonnegative_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, float) and math.isfinite(value):
        return max(int(value), 0)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return 0


@dataclass
class _ProjectionState:
    """Mutable budget/warning accumulator for one projection call."""

    label: str
    warnings: list[str] = field(default_factory=list)
    item_count: int = 0

    def warn(self, message: str) -> None:
        if len(self.warnings) < PROJECTION_MAX_WARNINGS:
            self.warnings.append(message)

    def count_item(self) -> bool:
        if self.item_count >= PROJECTION_MAX_ITEMS:
            self.warn(f"{self.label} projection omitted extra items")
            return False
        self.item_count += 1
        return True


def _projection_bounded_text(text: str, path: str, state: _ProjectionState) -> str:
    if len(text) <= PROJECTION_MAX_STRING_CHARS:
        return text
    state.warn(f"{path} string clipped")
    return text[:PROJECTION_MAX_STRING_CHARS]


def _projection_unsupported(obj: object, path: str, state: _ProjectionState) -> str:
    state.warn(f"{path} converted non-json {type(obj).__name__}")
    return f"<non-json {type(obj).__name__}>"


def _projection_sanitize_key(raw_key: object, path: str, state: _ProjectionState) -> str:
    if isinstance(raw_key, str):
        key = raw_key
    else:
        state.warn(f"{path} key converted to string")
        if raw_key is None or isinstance(raw_key, (bool, int, float)):
            key = str(raw_key)
        else:
            key = f"<non-json-key {type(raw_key).__name__}>"
    if not key:
        state.warn(f"{path} empty key renamed")
        key = "_"
    if len(key) > PROJECTION_MAX_KEY_CHARS:
        state.warn(f"{path} key clipped")
        key = key[:PROJECTION_MAX_KEY_CHARS]
    return key


def _projection_sanitize_leaf(obj: object, path: str, state: _ProjectionState) -> tuple[bool, object]:
    if obj is None or isinstance(obj, bool):
        return True, obj
    if isinstance(obj, str):
        return True, _projection_bounded_text(obj, path, state)
    if isinstance(obj, int):
        return True, obj
    if isinstance(obj, float):
        if math.isfinite(obj):
            return True, obj
        state.warn(f"{path} converted non-finite float")
        return True, str(obj)
    return False, None


def _projection_sanitize_mapping(
    mapping: Mapping[object, object],
    path: str,
    depth: int,
    state: _ProjectionState,
) -> dict[str, object]:
    sanitized: dict[str, object] = {}
    for raw_key, raw_value in mapping.items():
        if state.item_count >= PROJECTION_MAX_ITEMS:
            state.warn(f"{path} object omitted extra items")
            break
        key = _projection_sanitize_key(raw_key, f"{path}.key", state)
        if key == PROJECTION_WARNING_KEY:
            state.warn(f"{path}.{key} reserved key renamed")
            key = "_input_projection_warnings"
        if key in sanitized:
            state.warn(f"{path}.{key} duplicate key omitted")
            continue
        sanitized[key] = _projection_sanitize_value(raw_value, f"{path}.{key}", depth, state)
    return sanitized


def _projection_sanitize_sequence(
    obj: list[object] | tuple[object, ...],
    path: str,
    depth: int,
    state: _ProjectionState,
) -> list[object]:
    items: list[object] = []
    for index, item in enumerate(obj):
        if state.item_count >= PROJECTION_MAX_ITEMS:
            state.warn(f"{path} list omitted extra items")
            break
        items.append(_projection_sanitize_value(item, f"{path}[{index}]", depth + 1, state))
    return items


def _projection_sanitize_value(obj: object, path: str, depth: int, state: _ProjectionState) -> object:
    if not state.count_item():
        return None
    if depth > PROJECTION_MAX_DEPTH:
        state.warn(f"{path} exceeded max depth")
        return f"<max-depth {type(obj).__name__}>"
    handled, leaf = _projection_sanitize_leaf(obj, path, state)
    if handled:
        return leaf
    if isinstance(obj, Mapping):
        return _projection_sanitize_mapping(obj, path, depth + 1, state)
    if isinstance(obj, (list, tuple)):
        return _projection_sanitize_sequence(obj, path, depth, state)
    return _projection_unsupported(obj, path, state)


def json_safe_projection(
    value: object,
    *,
    label: str,
) -> dict[str, object]:
    """Return a bounded, JSON-safe projection mapping."""
    state = _ProjectionState(label=label)
    if value is None:
        result: dict[str, object] = {}
    elif isinstance(value, Mapping):
        result = _projection_sanitize_mapping(value, label, 0, state)
    else:
        result = {}
        state.warn(f"{label} projection replaced non-mapping {type(value).__name__}")
    if state.warnings:
        result[PROJECTION_WARNING_KEY] = state.warnings
    return result


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: dict[str, Any]
    call_id: str = ""


@dataclass(frozen=True)
class Control:
    kind: str
    body: str


@dataclass(frozen=True)
class ToolPlan:
    calls: list[ToolCall]
    control: Control | None
    protocol_error: str = ""
    protocol_error_kind: str = ""
    protocol_tool_name: str = ""
    alias_rewrite_count: int = 0
    arg_repair_counts: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolResult:
    call: ToolCall
    model_text: str
    truncated: bool = False
    presentation: Mapping[str, object] = field(default_factory=dict)
    audit: Mapping[str, object] = field(default_factory=dict)
    canonical: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        presentation = json_safe_projection(self.presentation, label="presentation")
        audit = json_safe_projection(self.audit, label="audit")
        canonical = json_safe_projection(self.canonical, label="canonical")
        object.__setattr__(
            self,
            "model_text",
            model_text_with_audit_markers(
                self.model_text,
                truncated=self.truncated,
                audit=audit,
            ),
        )
        object.__setattr__(self, "presentation", presentation)
        object.__setattr__(self, "audit", audit)
        object.__setattr__(self, "canonical", canonical)
