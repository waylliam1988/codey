"""Deterministic local gate for Ghost memory candidates."""

from __future__ import annotations

from dataclasses import dataclass

from codey.ghost.schema import (
    SENSITIVE_SIGNAL_DIAGNOSTIC,
    SIGNAL_KINDS,
    SIGNAL_SCOPES,
    GhostSignal,
    contains_sensitive_signal_text,
    quote_is_grounded,
)


MIN_CANDIDATE_CONFIDENCE = 0.45
STYLE_AUTO_ACCEPT_CONFIDENCE = 0.85

SIGNAL_CANDIDATE_TYPES = {
    "style_preference": "preference_candidate",
    "correction": "correction_candidate",
    "research_interest": "research_interest_candidate",
    "long_term_goal": "goal_candidate",
    "action_tendency": "action_tendency_candidate",
}
CANDIDATE_TYPES = tuple(SIGNAL_CANDIDATE_TYPES.values())
CANDIDATE_STATUSES = (
    "candidate",
    "accepted",
    "rejected",
    "expired",
    "superseded",
)

@dataclass(frozen=True)
class GhostGateDecision:
    candidate_type: str
    status: str
    reason: str

    @property
    def accepted(self) -> bool:
        return self.status == "accepted"

    def to_payload(self) -> dict[str, object]:
        return {
            "candidate_type": self.candidate_type,
            "status": self.status,
            "reason": self.reason,
            "accepted": self.accepted,
        }


def candidate_type_for_signal_kind(kind: str) -> str:
    return SIGNAL_CANDIDATE_TYPES.get(str(kind or "").strip().lower(), "")


class GhostMemoryGate:
    """Local-only quality and safety gate.

    ``accepted`` means the candidate is eligible for local Hebbian reinforce.
    It does not inject prompts or change model behavior.
    """

    def evaluate(
        self,
        signal: GhostSignal,
        *,
        session_id: str = "",
        run_id: str = "",
        project: str = "",
        user_text: str = "",
    ) -> GhostGateDecision:
        kind = str(getattr(signal, "kind", "") or "").strip().lower()
        candidate_type = candidate_type_for_signal_kind(kind) or "unknown_candidate"
        if kind not in SIGNAL_KINDS:
            return GhostGateDecision(candidate_type, "rejected", "invalid_signal_kind")

        scope = str(getattr(signal, "scope", "") or "").strip().lower()
        if scope not in SIGNAL_SCOPES:
            return GhostGateDecision(candidate_type, "rejected", "invalid_scope")
        if scope == "project" and not str(project or "").strip():
            return GhostGateDecision(candidate_type, "rejected", "missing_project_ref")
        if scope == "session" and not str(session_id or "").strip():
            return GhostGateDecision(candidate_type, "rejected", "missing_session_ref")
        if not str(session_id or run_id or "").strip():
            return GhostGateDecision(candidate_type, "rejected", "missing_provenance")

        summary = str(getattr(signal, "summary", "") or "").strip()
        evidence_quote = str(getattr(signal, "evidence_quote", "") or "").strip()
        if not summary:
            return GhostGateDecision(candidate_type, "rejected", "summary_required")
        if not evidence_quote:
            return GhostGateDecision(candidate_type, "rejected", "evidence_quote_required")
        if user_text and not quote_is_grounded(evidence_quote, user_text):
            return GhostGateDecision(candidate_type, "rejected", "evidence_quote_not_grounded")
        metadata = getattr(signal, "metadata", {}) or {}
        if contains_sensitive_signal_text(summary, evidence_quote, metadata):
            return GhostGateDecision(candidate_type, "rejected", SENSITIVE_SIGNAL_DIAGNOSTIC)

        confidence = _coerce_confidence(getattr(signal, "confidence", None))
        if confidence is None:
            return GhostGateDecision(candidate_type, "rejected", "invalid_confidence")
        if confidence < MIN_CANDIDATE_CONFIDENCE:
            return GhostGateDecision(candidate_type, "rejected", "confidence_below_candidate_threshold")

        if kind == "style_preference" and confidence >= STYLE_AUTO_ACCEPT_CONFIDENCE:
            return GhostGateDecision(candidate_type, "accepted", "high_confidence_style_preference")
        if kind == "correction":
            return GhostGateDecision(candidate_type, "candidate", "correction_requires_review")
        return GhostGateDecision(candidate_type, "candidate", "requires_review")


def _coerce_confidence(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return None
    if confidence < 0.0 or confidence > 1.0:
        return None
    return round(confidence, 4)
