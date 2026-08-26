"""Shared small redaction predicates for metadata boundaries.

Pure stdlib: these predicates decide whether a piece of text looks like a
secret marker or secret shape, so audit payloads can keep reason codes while
dropping anything secret-looking. Nothing here is research-specific.
"""

from __future__ import annotations

import re


_LATIN_SECRET_MARKER = (
    r"api[\s_-]?key|access[\s_-]?key|api[\s_-]?token|access[\s_-]?token|"
    r"auth[\s_-]?token|bearer[\s_-]?token|id[\s_-]?token|secrets?|client[\s_-]?secret|"
    r"password|passphrase|passwd|pwd|token|refresh[\s_-]?token|bearer|authorization|"
    r"cookie|credential|credentials|session[\s_-]?id|private[\s_-]?key|ssh[\s_-]?key|jwt"
)
SECRET_MARKER_RE = re.compile(
    rf"(?i)(?<![A-Za-z0-9])(?:{_LATIN_SECRET_MARKER})(?![A-Za-z0-9])|"
    r"(?:密钥|密码|令牌|凭证|私钥|访问令牌)"
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
_HIGH_ENTROPY_TOKEN_RE = re.compile(r"\b[A-Za-z0-9][A-Za-z0-9_\-./+=]{16,}\b")
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


def looks_high_entropy_secret(value: object) -> bool:
    """Return whether text carries a random token that looks like a secret.

    Tokens containing ``/`` are path- or URL-like references, and ordinary
    source paths are not secrets, so only slash-free random tokens qualify.
    """

    for token in _HIGH_ENTROPY_TOKEN_RE.findall(str(value or "")):
        if "/" in token:
            continue
        if _looks_like_secret_token(token):
            return True
    return False


def _looks_like_secret_token(token: str) -> bool:
    has_lower = any(char.islower() for char in token)
    has_upper = any(char.isupper() for char in token)
    if not (has_lower and has_upper):
        # Identifier-shaped values (hex ids, hashes, snake_case words) are
        # ordinary engineering artifacts, not secrets.
        return False
    varied = any(char.isdigit() for char in token) or any(not char.isalnum() for char in token)
    if not varied:
        return False
    unique_ratio = len(set(token)) / max(1, len(token))
    return unique_ratio >= 0.35


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
    "looks_high_entropy_secret",
    "looks_sensitive_code",
    "looks_secret_marker",
    "looks_secret_shape",
    "looks_sensitive_signal",
]
