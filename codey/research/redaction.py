"""Shared small redaction predicates for Research metadata boundaries."""

from __future__ import annotations

import re


_LATIN_SECRET_MARKER = (
    r"api[_ -]?key|access[_ -]?key|secret|client[_ -]?secret|"
    r"password|passwd|pwd|token|refresh[_ -]?token|bearer|authorization|"
    r"cookie|credential|credentials|session[_ -]?id|private[_ -]?key|ssh[_ -]?key|jwt"
)
SECRET_MARKER_RE = re.compile(
    rf"(?i)(?<![A-Za-z0-9])(?:{_LATIN_SECRET_MARKER})(?![A-Za-z0-9])|"
    r"(?:密钥|密码|令牌|私钥|访问令牌)"
)
SECRET_SHAPE_RE = re.compile(
    r"(?i)(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,}|"
    r"xox[baprs]-[A-Za-z0-9-]{16,}|AIza[0-9A-Za-z_-]{20,}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)


def looks_secret_marker(value: object) -> bool:
    text = str(value or "")
    return bool(SECRET_MARKER_RE.search(text))


def looks_secret_shape(value: object) -> bool:
    text = str(value or "")
    return bool(SECRET_SHAPE_RE.search(text))


def looks_sensitive_signal(value: object) -> bool:
    return looks_secret_marker(value) or looks_secret_shape(value)


__all__ = [
    "SECRET_MARKER_RE",
    "SECRET_SHAPE_RE",
    "looks_secret_marker",
    "looks_secret_shape",
    "looks_sensitive_signal",
]
