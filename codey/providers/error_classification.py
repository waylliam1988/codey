"""Deterministic provider error classification.

Separates transient network faults (safe to retry) from context overflow and
output-length truncation (must compact/roll over, never blind-retry) and from
auth/quota/fatal errors.
"""

from __future__ import annotations

from collections.abc import Mapping


class ProviderErrorKind:
    TRANSIENT = "transient"
    CONTEXT_OVERFLOW = "context_overflow"
    OUTPUT_LENGTH = "output_length"
    AUTH = "auth"
    QUOTA = "quota"
    FATAL = "fatal"


class ContextOverflowError(RuntimeError):
    pass


class OutputLengthError(RuntimeError):
    def __init__(self, message: str = "model output truncated (finish_reason=length)") -> None:
        super().__init__(message)


class RequestPrepError(RuntimeError):
    """Pre-send request preparation failed; nothing was sent.

    Compaction/estimation failures before any HTTP call settle as NOT_SENT
    (safe: no usable reply could exist) but must never trigger the context
    overflow rollover retry, which is reserved for genuine overflows.
    """


_OVERFLOW_MARKERS = (
    "context length",
    "context_length",
    "context window",
    "context_window",
    "maximum context",
    "max context",
    "too many tokens",
    "token limit",
    "tokens exceed",
    "exceeds the model's context",
    "model's maximum context",
    "context size",
    "input too long",
    "prompt too long",
    "prompt is too long",
)

_QUOTA_MARKERS = (
    "insufficient_quota",
    "insufficient quota",
    "quota exceeded",
    "billing",
    "payment required",
)

def is_context_overflow_message(text: str) -> bool:
    folded = str(text or "").lower()
    if not folded:
        return False
    return any(marker in folded for marker in _OVERFLOW_MARKERS)


def classify_http_error(status: int, body: str) -> str:
    if status in (401, 403):
        return ProviderErrorKind.AUTH
    if status == 402 or is_quota_message(body):
        return ProviderErrorKind.QUOTA
    if status == 413 or is_context_overflow_message(body):
        return ProviderErrorKind.CONTEXT_OVERFLOW
    if status in (429, 502, 503, 504):
        return ProviderErrorKind.TRANSIENT
    if 500 <= status <= 599:
        return ProviderErrorKind.TRANSIENT
    return ProviderErrorKind.FATAL


def is_quota_message(text: str) -> bool:
    folded = str(text or "").lower()
    return any(marker in folded for marker in _QUOTA_MARKERS)


def classify_openai_choice(choice: Mapping[str, object]) -> str:
    finish = str(choice.get("finish_reason") or "").lower()
    if finish == "length":
        message = choice.get("message")
        content = ""
        if isinstance(message, Mapping):
            content = str(message.get("content") or "")
        if is_context_overflow_message(content):
            return ProviderErrorKind.CONTEXT_OVERFLOW
        return ProviderErrorKind.OUTPUT_LENGTH
    if finish == "content_filter":
        return ProviderErrorKind.FATAL
    return ProviderErrorKind.TRANSIENT


__all__ = [
    "ContextOverflowError",
    "OutputLengthError",
    "ProviderErrorKind",
    "RequestPrepError",
    "classify_http_error",
    "classify_openai_choice",
    "is_context_overflow_message",
    "is_quota_message",
]
