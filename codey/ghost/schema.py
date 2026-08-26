"""Bounded schema for Ghost signal candidates.

Ghost signals are candidates, not accepted memory.  They capture only explicit
user requests to remember a preference, correction, research interest, goal, or
action tendency.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from codey.ghost.numbers import coerce_unit_float
from codey.redaction import looks_high_entropy_secret, looks_sensitive_signal

SCHEMA_VERSION = 1
MAX_SIGNALS_PER_TURN = 5
MAX_SIGNAL_TEXT_CHARS = 600
MAX_SIGNAL_QUOTE_CHARS = 240
MAX_DIAGNOSTIC_CHARS = 240
MAX_EXTRACTOR_USER_CHARS = 3_000
MAX_EXTRACTOR_ASSISTANT_CHARS = 1_000

SIGNAL_KINDS = (
    "style_preference",
    "correction",
    "research_interest",
    "long_term_goal",
    "action_tendency",
)
SIGNAL_SCOPES = ("user", "project", "session")
SIGNAL_SOURCES = ("llm_extractor", "manual", "test")
TRUNCATED_TEXT_SUFFIX = "..."
SENSITIVE_SIGNAL_DIAGNOSTIC = "sensitive_signal_rejected"


@dataclass(frozen=True)
class GhostSignal:
    kind: str
    scope: str
    summary: str
    evidence_quote: str
    confidence: float
    source: str = "llm_extractor"
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


@dataclass(frozen=True)
class GhostSignalParseResult:
    signals: tuple[GhostSignal, ...] = ()
    diagnostics: tuple[str, ...] = ()
    ok: bool = True
    raw_text_chars: int = 0
    provider_id: str = ""

    @property
    def has_signal(self) -> bool:
        return bool(self.signals)


def clip_signal_text(value: object, limit: int = MAX_SIGNAL_TEXT_CHARS) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= len(TRUNCATED_TEXT_SUFFIX):
        return text[:limit]
    return text[: limit - len(TRUNCATED_TEXT_SUFFIX)].rstrip() + TRUNCATED_TEXT_SUFFIX


def clip_grounded_quote(value: object, limit: int = MAX_SIGNAL_QUOTE_CHARS) -> str:
    """Clip a quote without adding characters that were not in the user text."""

    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if limit <= 0:
        return ""
    return text[:limit].strip()


def quote_is_grounded(quote: object, user_text: object) -> bool:
    quote_norm = _normalize_for_grounding(quote)
    user_norm = _normalize_for_grounding(user_text)
    return bool(quote_norm and user_norm and quote_norm in user_norm)


def signals_from_payload(
    payload: object,
    *,
    user_text: str,
    source: str = "llm_extractor",
) -> tuple[tuple[GhostSignal, ...], tuple[str, ...]]:
    if not isinstance(payload, dict):
        return (), ("invalid_schema: expected object",)
    raw_signals = payload.get("signals")
    if raw_signals is None and str(payload.get("kind") or "").strip().lower() in {"no_signal", "none"}:
        raw_signals = []
    if not isinstance(raw_signals, list):
        return (), ("invalid_schema: signals must be a list",)

    signals: list[GhostSignal] = []
    diagnostics: list[str] = []
    for index, raw in enumerate(raw_signals):
        if len(signals) >= MAX_SIGNALS_PER_TURN:
            diagnostics.append(f"too_many_signals: kept first {MAX_SIGNALS_PER_TURN}")
            break
        signal, row_diagnostics = signal_from_mapping(
            raw,
            user_text=user_text,
            source=source,
            index=index,
        )
        diagnostics.extend(row_diagnostics)
        if signal is not None:
            signals.append(signal)
    return tuple(signals), tuple(_clip_diagnostic(item) for item in diagnostics)


def signal_from_mapping(
    raw: object,
    *,
    user_text: str,
    source: str = "llm_extractor",
    index: int = 0,
) -> tuple[GhostSignal | None, tuple[str, ...]]:
    if not isinstance(raw, dict):
        return None, (f"signal[{index}]: expected object",)

    kind = str(raw.get("kind") or "").strip().lower()
    if kind in {"", "no_signal", "none"}:
        return None, ()
    if kind not in SIGNAL_KINDS:
        return None, (f"signal[{index}]: unknown kind {kind or '<empty>'}",)

    scope = str(raw.get("scope") or "").strip().lower()
    if scope not in SIGNAL_SCOPES:
        return None, (f"signal[{index}]: invalid scope {scope or '<empty>'}",)

    summary = clip_signal_text(raw.get("summary"))
    if not summary:
        return None, (f"signal[{index}]: summary required",)

    raw_quote = str(raw.get("evidence_quote") or "").strip()
    if not quote_is_grounded(raw_quote, user_text):
        return None, (f"signal[{index}]: evidence_quote not grounded in user text",)
    evidence_quote = clip_grounded_quote(raw_quote)
    if not evidence_quote:
        return None, (f"signal[{index}]: evidence_quote required",)

    confidence = _coerce_confidence(raw.get("confidence"))
    if confidence is None:
        return None, (f"signal[{index}]: invalid confidence",)

    clean_source = str(source or raw.get("source") or "llm_extractor").strip()
    if clean_source not in SIGNAL_SOURCES:
        clean_source = "llm_extractor"

    metadata = raw.get("metadata")
    clean_metadata = _clean_metadata(metadata)
    if contains_sensitive_signal_text(summary, evidence_quote, clean_metadata):
        return None, (f"signal[{index}]: {SENSITIVE_SIGNAL_DIAGNOSTIC}",)
    return (
        GhostSignal(
            kind=kind,
            scope=scope,
            summary=summary,
            evidence_quote=evidence_quote,
            confidence=confidence,
            source=clean_source,
            metadata=clean_metadata,
        ),
        (),
    )


def _coerce_confidence(value: object) -> float | None:
    return coerce_unit_float(value, digits=4)


def _clean_metadata(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    clean: dict[str, object] = {}
    for key, item in value.items():
        text_key = clip_signal_text(key, 80)
        if not text_key:
            continue
        if isinstance(item, (str, int, float, bool)) or item is None:
            clean[text_key] = clip_signal_text(item, 160) if isinstance(item, str) else item
        if len(clean) >= 8:
            break
    return clean


def contains_sensitive_signal_text(*values: object) -> bool:
    """Reject secret markers, provider key shapes, and high-entropy tokens.

    The semantics are owned by :mod:`codey.redaction`; path-like tokens stay
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
        if looks_sensitive_signal(text) or looks_high_entropy_secret(text):
            return True
    return False


def _clip_diagnostic(value: object) -> str:
    return clip_signal_text(value, MAX_DIAGNOSTIC_CHARS)


def _normalize_for_grounding(value: object) -> str:
    return " ".join(str(value or "").split()).casefold()
