from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from codey.codemode.types import CodeModeLimits, CodeModeProgram
from codey.runtime.core.models import canonical_json_mapping

_DENIED_SOURCE = re.compile(
    r"(?:\bprocess\b|\brequire\b|\bimport\b|\bfetch\b|\beval\b|"
    r"\bglobalThis\b|\b__dirname\b|\b__filename\b|\bchild_process\b|\bfs\b|\bconstructor\b|"
    r"tools\s*(?:\.\s*codemode|\[\s*['\"]codemode['\"]\s*\])\b)",
    re.IGNORECASE,
)


class CodeModeInputError(ValueError):
    def __init__(self, message: str, *, code: str = "invalid_args") -> None:
        super().__init__(message)
        self.code = code


def _check_input_numbers(value: object) -> None:
    if type(value) is int and abs(value) > 9_007_199_254_740_991:
        raise CodeModeInputError("codemode inputs integers must be JavaScript safe integers")
    if isinstance(value, Mapping):
        for item in value.values():
            _check_input_numbers(item)
    elif isinstance(value, list):
        for item in value:
            _check_input_numbers(item)


def codemode_error_text(error: str, *, description: str) -> str:
    """Render a failure with its already captured contract, without API repair."""
    return f"{error}\nCurrent CodeMode contract:\n{description}"


def parse_codemode_args(args: Mapping[str, Any], *, limits: CodeModeLimits | None = None) -> CodeModeProgram:
    if not isinstance(args, Mapping):
        raise CodeModeInputError("codemode args must be an object")
    active_limits = limits or CodeModeLimits()
    code = args.get("code")
    if not isinstance(code, str) or not code.strip():
        raise CodeModeInputError("codemode code must be a non-empty string")
    try:
        inputs = canonical_json_mapping(args.get("inputs", {}))
        _check_input_numbers(inputs)
        input_bytes = len(json.dumps(inputs, ensure_ascii=False, separators=(",", ":"),
                                    allow_nan=False).encode("utf-8")) if inputs else 0
        source_bytes = len(code.encode("utf-8"))
    except (TypeError, ValueError, RecursionError) as exc:
        raise CodeModeInputError(f"codemode inputs must be a strict JSON object: {exc}") from exc
    if source_bytes + input_bytes > active_limits.source_bytes:
        raise CodeModeInputError("codemode source and inputs exceed source byte limit", code="source_limit")
    language = str(args.get("language", "javascript") or "javascript").strip().lower()
    if language not in {"javascript", "js"}:
        raise CodeModeInputError("codemode language must be javascript")
    denied = _DENIED_SOURCE.search(code) or re.search(r"\bFunction\s*\(", code)
    if denied:
        access = ("recursive invocation" if denied.group(0).lower().startswith("tools")
                  else f"direct host access token '{denied.group(0)}'")
        raise CodeModeInputError(
            f"codemode rejected before execution: {access} is denied.", code="sandbox_denied"
        )
    return CodeModeProgram(code=code, language="javascript", inputs=inputs)


def parse_codemode_text(text: str, *, limits: CodeModeLimits | None = None) -> CodeModeProgram:
    if not isinstance(text, str):
        raise ValueError("codemode text must be a string")
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError as exc:
        if exc.msg == "Extra data":
            raise ValueError("codemode text must contain exactly one JSON object") from exc
        raise ValueError(f"codemode text is not valid JSON: {exc.msg}") from exc
    if not isinstance(decoded, dict) or set(decoded) != {"tool", "args"}:
        raise ValueError("codemode text must contain exactly one tool and args object")
    if decoded.get("tool") != "codemode":
        raise ValueError("codemode text must name the codemode tool")
    args = decoded.get("args")
    if not isinstance(args, dict):
        raise ValueError("codemode args must be an object")
    return parse_codemode_args(args, limits=limits)
