"""Provider-send admission for model-visible memory context.

memory projections are candidates. This module is the last synchronous check
before a shared-kernel provider send and records only bounded refs/digests in
the resulting ContextEpoch.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from codey.utils.refs import digest_text
from codey.workspace.context_epoch import (
    PROVIDER_TURN_ADMISSION,
    ContextAdmission,
    ContextEpoch,
    admission_from_rendered_source,
    context_epoch_id,
)
from codey.workspace.context_source import ContextCandidate

# Local projection character cap, independent of model/output configuration.
MEMORY_TOTAL_BUDGET_CHARS = 6000


@dataclass(frozen=True)
class ContextRejection:
    key: str
    reason: str


@dataclass(frozen=True)
class ContextAdmissionResult:
    epoch: ContextEpoch
    accepted: tuple[ContextCandidate, ...] = ()
    rejected: tuple[ContextRejection, ...] = ()


class ContextAdmissionError(ValueError):
    """Raised before provider-send when required memory context is unsafe."""

    def __init__(self, rejections: Iterable[ContextRejection]) -> None:
        self.rejections = tuple(rejections)
        summary = ", ".join(f"{row.key}:{row.reason}" for row in self.rejections)
        super().__init__(f"memory context admission rejected: {summary or 'unknown'}")


def admit_context(
    prompt: str,
    sources: Iterable[ContextCandidate] = (),
    *,
    request_scope: str,
    require_all: bool = False,
    admit_hints: bool = True,
    as_of: str | None = None,
) -> ContextAdmissionResult:
    """Validate memory placement against the exact outbound prompt.

    A source is admitted only when it is present byte-for-byte in ``prompt``,
    within its declared budget, and within the request scope. On the initial
    turn ``require_all`` makes a rejected candidate fail closed instead of
    silently concatenating it.
    """
    prompt_text = str(prompt or "")
    accepted: list[ContextCandidate] = []
    admissions: list[ContextAdmission] = []
    rejected: list[ContextRejection] = []
    seen_keys: set[str] = set()
    used_chars = 0
    expected_scope = str(request_scope or "").strip()
    effective_as_of = _timestamp(as_of) if as_of else datetime.now(UTC)
    if effective_as_of is None:
        raise ValueError("as_of must be an ISO-8601 timestamp")
    for source in sources:
        key = str(source.key or "").strip()
        text = str(source.text or "")
        if not text:
            continue
        if not key or key in seen_keys:
            rejected.append(ContextRejection(key or "<empty>", "invalid_or_duplicate_key"))
            continue
        seen_keys.add(key)
        if not expected_scope:
            rejected.append(ContextRejection(key, "missing_request_scope"))
            continue
        if not admit_hints:
            rejected.append(ContextRejection(key, "hints_disabled"))
            continue
        if source.key.startswith("memory_") and not source.source_refs:
            rejected.append(ContextRejection(key, "missing_provenance"))
            continue
        expires_at = str(source.expires_at or "").strip()
        if expires_at:
            expires = _timestamp(expires_at)
            if expires is None:
                rejected.append(ContextRejection(key, "invalid_expiry"))
                continue
            if expires <= effective_as_of:
                rejected.append(ContextRejection(key, "expired"))
                continue
        budget = max(0, int(source.budget or 0))
        scope = str(source.scope or "").strip()
        if budget <= 0 or len(text) > budget:
            rejected.append(ContextRejection(key, "over_budget"))
            continue
        if scope not in {expected_scope, "user"}:
            rejected.append(ContextRejection(key, "scope_mismatch"))
            continue
        if not scope:
            rejected.append(ContextRejection(key, "missing_scope"))
            continue
        if text not in prompt_text:
            rejected.append(ContextRejection(key, "not_in_provider_payload"))
            continue
        cost = len(text) + (2 if accepted else 0)
        if used_chars + cost > MEMORY_TOTAL_BUDGET_CHARS:
            rejected.append(ContextRejection(key, "combined_over_budget"))
            continue
        admission = admission_from_rendered_source(source, admission_reason=PROVIDER_TURN_ADMISSION)
        if admission is None:
            rejected.append(ContextRejection(key, "invalid_source"))
            continue
        accepted.append(source)
        used_chars += cost
        admissions.append(admission)
    if require_all and rejected:
        raise ContextAdmissionError(rejected)
    return ContextAdmissionResult(
        epoch=ContextEpoch(epoch_id=context_epoch_id(prompt_text), admissions=tuple(admissions)),
        accepted=tuple(accepted),
        rejected=tuple(rejected),
    )


def request_scope(project: object = "", session_id: object = "") -> str:
    """Bind a bounded identity to the complete project/session, never a prefix."""
    project_text = str(project or "").strip()
    if project_text:
        return f"project:{digest_text(project_text)}"
    session_text = str(session_id or "").strip()
    if session_text:
        return f"session:{digest_text(session_text)}"
    return "request"


def source_from_value(*, key: str, text: object, budget: int, scope: str,
                      capability_id: str = "local_context", expires_at: str = "") -> ContextCandidate | None:
    value = str(text or "").strip()
    if not value:
        return None
    return ContextCandidate(
        key=key,
        text=value,
        budget=budget,
        scope=scope,
        capability_id=capability_id,
        expires_at=expires_at,
    )


def _timestamp(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


__all__ = [
    "ContextAdmissionError",
    "ContextRejection",
    "ContextAdmissionResult",
    "ContextCandidate",
    "admit_context",
    "request_scope",
    "source_from_value",
    ]
