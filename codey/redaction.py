"""Shared small redaction predicates for metadata boundaries.

Pure stdlib: these predicates decide whether a piece of text looks like a
secret marker or secret shape, so audit payloads can keep reason codes while
dropping anything secret-looking. Nothing here is research-specific.
"""

from __future__ import annotations

import re


_LATIN_SECRET_MARKER = (
    r"api[_ -]?key|access[_ -]?key|api[_ -]?token|access[_ -]?token|"
    r"auth[_ -]?token|bearer[_ -]?token|id[_ -]?token|secret|client[_ -]?secret|"
    r"password|passphrase|passwd|pwd|token|refresh[_ -]?token|bearer|authorization|"
    r"cookie|credential|credentials|session[_ -]?id|private[_ -]?key|ssh[_ -]?key|jwt"
)
SECRET_MARKER_RE = re.compile(
    rf"(?i)(?<![A-Za-z0-9])(?:{_LATIN_SECRET_MARKER})(?![A-Za-z0-9])|"
    r"(?:密钥|密码|令牌|私钥|访问令牌)"
)
SECRET_SHAPE_RE = re.compile(
    r"(?i)(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,}|"
    r"xox[baprs]-[A-Za-z0-9-]{16,}|AIza[0-9A-Za-z_-]{20,}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----)|"
    r"(?-i:(?<![A-Za-z0-9])AKIA[0-9A-Z]{16}(?![A-Za-z0-9])|"
    r"(?<![A-Za-z0-9])github_pat_[A-Za-z0-9_]{22,}|"
    r"(?<![A-Za-z0-9])(?:sk|rk)_(?:live|test)_[A-Za-z0-9_-]{16,})"
)
_CODE_SEPARATOR_RE = re.compile(r"[^a-z0-9]+")
_CJK_SECRET_CODE_RE = re.compile(r"(?:密钥|密码|令牌|私钥|访问令牌)")
_SENSITIVE_CODE_COMPONENTS = frozenset({
    "bearer",
    "cookie",
    "credential",
    "credentials",
    "jwt",
    "passwd",
    "password",
    "passphrase",
    "pwd",
    "secret",
})
_SENSITIVE_CODE_PHRASES = frozenset({
    ("access", "key"),
    ("access", "token"),
    ("api", "key"),
    ("api", "token"),
    ("auth", "token"),
    ("bearer", "token"),
    ("client", "secret"),
    ("id", "token"),
    ("private", "key"),
    ("refresh", "token"),
    ("session", "id"),
    ("ssh", "key"),
})
_SENSITIVE_CODE_COMPOUNDS = frozenset({
    "accesskey",
    "accesstoken",
    "apikey",
    "apitoken",
    "authtoken",
    "bearertoken",
    "clientsecret",
    "idtoken",
    "privatekey",
    "refreshtoken",
    "sessionid",
    "sshkey",
})
_SENSITIVE_CODE_EXACT = frozenset({"authorization", "token"})


def looks_secret_marker(value: object) -> bool:
    text = str(value or "")
    return bool(SECRET_MARKER_RE.search(text))


def looks_secret_shape(value: object) -> bool:
    text = str(value or "")
    return bool(SECRET_SHAPE_RE.search(text))


def looks_sensitive_signal(value: object) -> bool:
    return looks_secret_marker(value) or looks_secret_shape(value)


def looks_sensitive_code(value: object) -> bool:
    """Return whether an audit code looks like a secret marker or secret shape.

    Audit codes often contain safe marker words as context, for example
    ``token_budget_exceeded`` or ``authorization_required``. This predicate
    rejects direct secret shapes and high-risk marker components while preserving
    those ordinary reason-code forms.
    """

    text = str(value or "").strip()
    if not text:
        return False
    if looks_secret_shape(text):
        return True
    if _CJK_SECRET_CODE_RE.search(text):
        return True
    normalized = _CODE_SEPARATOR_RE.sub("_", text.casefold()).strip("_")
    if not normalized:
        return False
    parts = tuple(part for part in normalized.split("_") if part)
    if normalized in _SENSITIVE_CODE_EXACT:
        return True
    if any(part in _SENSITIVE_CODE_COMPONENTS for part in parts):
        return True
    compact = "".join(parts)
    if any(item in compact for item in _SENSITIVE_CODE_COMPOUNDS):
        return True
    return any(pair in _SENSITIVE_CODE_PHRASES for pair in zip(parts, parts[1:]))


__all__ = [
    "SECRET_MARKER_RE",
    "SECRET_SHAPE_RE",
    "looks_sensitive_code",
    "looks_secret_marker",
    "looks_secret_shape",
    "looks_sensitive_signal",
]
