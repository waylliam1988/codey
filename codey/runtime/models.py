from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


TRUNCATED_RESULT_NOTICE = (
    "[truncated result: omitted content may contain relevant "
    "errors or code. Do not assume omitted content is clean. "
    "Use narrower grep/read_file offsets or rerun a narrower "
    "command if needed.]"
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
    footer = _managed_output_footer(managed)
    if footer and footer not in text:
        text = f"{text}\n{footer}"
    return text


def _managed_output_footer(value: object) -> str:
    managed = normalized_managed_output(value)
    if not managed:
        return ""
    return (
        "[full output retained locally: "
        f"handle={managed['handle']}, "
        f"original_bytes={managed['original_bytes']}, "
        f"stored_bytes={managed['stored_bytes']}, "
        f"sha256={managed['sha256']}; "
        "handle is for local audit/export, not a tool.]"
    )


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


def json_safe_projection(
    value: object,
    *,
    label: str,
) -> dict[str, object]:
    """Return a bounded, JSON-safe projection mapping."""

    warnings: list[str] = []
    item_count = 0

    def warn(message: str) -> None:
        if len(warnings) < PROJECTION_MAX_WARNINGS:
            warnings.append(message)

    def count_item() -> bool:
        nonlocal item_count
        if item_count >= PROJECTION_MAX_ITEMS:
            warn(f"{label} projection omitted extra items")
            return False
        item_count += 1
        return True

    def bounded_text(text: str, path: str) -> str:
        if len(text) <= PROJECTION_MAX_STRING_CHARS:
            return text
        warn(f"{path} string clipped")
        return text[:PROJECTION_MAX_STRING_CHARS]

    def unsupported(obj: object, path: str) -> str:
        warn(f"{path} converted non-json {type(obj).__name__}")
        return f"<non-json {type(obj).__name__}>"

    def sanitize_key(raw_key: object, path: str) -> str:
        if isinstance(raw_key, str):
            key = raw_key
        else:
            warn(f"{path} key converted to string")
            if raw_key is None or isinstance(raw_key, (bool, int, float)):
                key = str(raw_key)
            else:
                key = f"<non-json-key {type(raw_key).__name__}>"
        if not key:
            warn(f"{path} empty key renamed")
            key = "_"
        if len(key) > PROJECTION_MAX_KEY_CHARS:
            warn(f"{path} key clipped")
            key = key[:PROJECTION_MAX_KEY_CHARS]
        return key

    def sanitize(obj: object, path: str, depth: int) -> object:
        if not count_item():
            return None
        if depth > PROJECTION_MAX_DEPTH:
            warn(f"{path} exceeded max depth")
            return f"<max-depth {type(obj).__name__}>"
        if obj is None or isinstance(obj, bool):
            return obj
        if isinstance(obj, str):
            return bounded_text(obj, path)
        if isinstance(obj, int):
            return obj
        if isinstance(obj, float):
            if math.isfinite(obj):
                return obj
            warn(f"{path} converted non-finite float")
            return str(obj)
        if isinstance(obj, Mapping):
            return sanitize_mapping(obj, path, depth + 1)
        if isinstance(obj, (list, tuple)):
            items: list[object] = []
            for index, item in enumerate(obj):
                if item_count >= PROJECTION_MAX_ITEMS:
                    warn(f"{path} list omitted extra items")
                    break
                items.append(sanitize(item, f"{path}[{index}]", depth + 1))
            return items
        return unsupported(obj, path)

    def sanitize_mapping(
        mapping: Mapping[object, object],
        path: str,
        depth: int,
    ) -> dict[str, object]:
        sanitized: dict[str, object] = {}
        for raw_key, raw_value in mapping.items():
            if item_count >= PROJECTION_MAX_ITEMS:
                warn(f"{path} object omitted extra items")
                break
            key = sanitize_key(raw_key, f"{path}.key")
            if key == PROJECTION_WARNING_KEY:
                warn(f"{path}.{key} reserved key renamed")
                key = "_input_projection_warnings"
            if key in sanitized:
                warn(f"{path}.{key} duplicate key omitted")
                continue
            sanitized[key] = sanitize(raw_value, f"{path}.{key}", depth)
        return sanitized

    if value is None:
        result: dict[str, object] = {}
    elif isinstance(value, Mapping):
        result = sanitize_mapping(value, label, 0)
    else:
        result = {}
        warn(f"{label} projection replaced non-mapping {type(value).__name__}")
    if warnings:
        result[PROJECTION_WARNING_KEY] = warnings
    return result


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: dict[str, Any]


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
