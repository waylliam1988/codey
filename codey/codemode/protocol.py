from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from codey.codemode.types import CodeModeLimits, CodeModeProgram

_DENIED_SOURCE = re.compile(
    r"(?:\bprocess\b|\brequire\b|\bimport\b|\bfetch\b|\beval\b|\bFunction\b|"
    r"\bglobalThis\b|\b__dirname\b|\b__filename\b|\bchild_process\b|\bfs\b|\bconstructor\b|"
    r"tools\s*(?:\.\s*codemode|\[\s*['\"]codemode['\"]\s*\])\b)",
    re.IGNORECASE,
)


def parse_codemode_args(args: Mapping[str, Any], *, limits: CodeModeLimits | None = None) -> CodeModeProgram:
    if not isinstance(args, Mapping):
        raise ValueError("codemode args must be an object")
    active_limits = limits or CodeModeLimits()
    code = args.get("code")
    if not isinstance(code, str) or not code.strip():
        raise ValueError("codemode code must be a non-empty string")
    if len(code.encode("utf-8")) > active_limits.source_bytes:
        raise ValueError("codemode source exceeds source byte limit")
    language = str(args.get("language", "javascript") or "javascript").strip().lower()
    if language not in {"javascript", "js"}:
        raise ValueError("codemode language must be javascript")
    if _DENIED_SOURCE.search(code):
        raise ValueError("codemode recursive or host access is denied")
    return CodeModeProgram(code=code, language="javascript")


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
