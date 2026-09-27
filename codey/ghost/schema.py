"""Bounded schema for Ghost signal candidates.

Ghost signals are candidates, not accepted memory.  They capture only explicit
user requests to remember a preference, correction, research interest, goal, or
action tendency.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from codey.policies.redaction import looks_prompt_visible_secret

SCHEMA_VERSION = 1
MAX_SIGNAL_TEXT_CHARS = 600
MAX_SIGNAL_QUOTE_CHARS = 240

SIGNAL_KINDS = (
    "style_preference",
    "correction",
    "research_interest",
    "long_term_goal",
    "action_tendency",
)
SIGNAL_SCOPES = ("user", "project", "session")
TRUNCATED_TEXT_SUFFIX = "..."
SENSITIVE_SIGNAL_DIAGNOSTIC = "sensitive_signal_rejected"


@dataclass(frozen=True)
class GhostSignal:
    kind: str
    scope: str
    summary: str
    evidence_quote: str
    confidence: float
    source: str
    metadata: dict[str, object] = field(default_factory=dict)

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "kind": self.kind,
            "scope": self.scope,
            "summary": self.summary,
            "evidence_quote": self.evidence_quote,
            "confidence": self.confidence,
            "source": self.source,
        }
        if self.metadata:
            payload["metadata"] = dict(self.metadata)
        return payload


def clip_signal_text(value: object, limit: int = MAX_SIGNAL_TEXT_CHARS) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= len(TRUNCATED_TEXT_SUFFIX):
        return text[:limit]
    return text[: limit - len(TRUNCATED_TEXT_SUFFIX)].rstrip() + TRUNCATED_TEXT_SUFFIX


def quote_is_grounded(quote: object, user_text: object) -> bool:
    quote_norm = _normalize_for_grounding(quote)
    user_norm = _normalize_for_grounding(user_text)
    return bool(quote_norm and user_norm and quote_norm in user_norm)


def contains_sensitive_signal_text(*values: object) -> bool:
    """Reject secret markers, provider key shapes, and high-entropy tokens.

    The semantics are owned by :mod:`codey.policies.redaction`; path-like tokens stay
    exempt so ordinary source references never reject a signal.
    """

    for value in values:
        if isinstance(value, dict):
            if contains_sensitive_signal_text(*value.keys(), *value.values()):
                return True
            continue
        if isinstance(value, (list, tuple, set)):
            if contains_sensitive_signal_text(*value):
                return True
            continue
        text = str(value or "")
        if not text:
            continue
        if looks_prompt_visible_secret(text):
            return True
    return False


def _normalize_for_grounding(value: object) -> str:
    return " ".join(str(value or "").split()).casefold()
