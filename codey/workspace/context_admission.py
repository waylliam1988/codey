"""Provider-send admission for model-visible Ghost context.

Ghost projections are candidates. This module is the last synchronous check
before a shared-kernel provider send and records only bounded refs/digests in
the resulting ContextEpoch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from codey.workspace.context_epoch import (
    PROVIDER_TURN_ADMISSION,
    ContextAdmission,
    ContextEpoch,
    admission_from_rendered_source,
    context_epoch_id,
)


@dataclass(frozen=True)
class GhostContextSource:
    key: str
    text: str
    budget: int
    scope: str
    capability_id: str = "local_context"
    admission_reason: str = PROVIDER_TURN_ADMISSION


@dataclass(frozen=True)
class GhostAdmissionRejection:
    key: str
    reason: str


@dataclass(frozen=True)
class GhostAdmissionResult:
    epoch: ContextEpoch
    accepted: tuple[GhostContextSource, ...] = ()
    rejected: tuple[GhostAdmissionRejection, ...] = ()


class GhostAdmissionError(ValueError):
    """Raised before provider-send when required Ghost context is unsafe."""

    def __init__(self, rejections: Iterable[GhostAdmissionRejection]) -> None:
        self.rejections = tuple(rejections)
        summary = ", ".join(f"{row.key}:{row.reason}" for row in self.rejections)
        super().__init__(f"Ghost context admission rejected: {summary or 'unknown'}")


def admit_ghost_context(
    prompt: str,
    sources: Iterable[GhostContextSource] = (),
    *,
    request_scope: str = "",
    require_all: bool = False,
    admit_hints: bool = True,
) -> GhostAdmissionResult:
    """Validate Ghost placement against the exact outbound prompt.

    A source is admitted only when it is present byte-for-byte in ``prompt``,
    within its declared budget, and within the request scope. On the initial
    turn ``require_all`` makes a rejected candidate fail closed instead of
    silently concatenating it.
    """
    prompt_text = str(prompt or "")
    accepted: list[GhostContextSource] = []
    admissions: list[ContextAdmission] = []
    rejected: list[GhostAdmissionRejection] = []
    seen_keys: set[str] = set()
    expected_scope = str(request_scope or "").strip()
    for source in sources:
        key = str(source.key or "").strip()
        text = str(source.text or "")
        if not text:
            continue
        if not key or key in seen_keys:
            rejected.append(GhostAdmissionRejection(key or "<empty>", "invalid_or_duplicate_key"))
            continue
        seen_keys.add(key)
        if not admit_hints:
            rejected.append(GhostAdmissionRejection(key, "hints_disabled"))
            continue
        budget = max(0, int(source.budget or 0))
        scope = str(source.scope or "").strip()
        if budget <= 0 or len(text) > budget:
            rejected.append(GhostAdmissionRejection(key, "over_budget"))
            continue
        if expected_scope and scope not in {expected_scope, "user", "global"}:
            rejected.append(GhostAdmissionRejection(key, "scope_mismatch"))
            continue
        if not scope:
            rejected.append(GhostAdmissionRejection(key, "missing_scope"))
            continue
        if text not in prompt_text:
            rejected.append(GhostAdmissionRejection(key, "not_in_provider_payload"))
            continue
        admission = admission_from_rendered_source(source, admission_reason=PROVIDER_TURN_ADMISSION)
        if admission is None:
            rejected.append(GhostAdmissionRejection(key, "invalid_source"))
            continue
        accepted.append(source)
        admissions.append(admission)
    if require_all and rejected:
        raise GhostAdmissionError(rejected)
    return GhostAdmissionResult(
        epoch=ContextEpoch(epoch_id=context_epoch_id(prompt_text), admissions=tuple(admissions)),
        accepted=tuple(accepted),
        rejected=tuple(rejected),
    )


def request_scope(project: object = "", session_id: object = "") -> str:
    """Return a bounded scope identity shared by one task's Ghost sources."""
    project_text = str(project or "").strip()
    if project_text:
        return f"project:{project_text[:140]}"
    session_text = str(session_id or "").strip()
    if session_text:
        return f"session:{session_text[:140]}"
    return "request"


def sources_from_values(
    *,
    directive: object = "",
    continuity: object = "",
    experiences: object = "",
    scope: str,
    budgets: tuple[int, int, int] = (900, 900, 1800),
) -> tuple[GhostContextSource, ...]:
    values = (
        ("ghost_directive", directive, budgets[0]),
        ("ghost_continuity", continuity, budgets[1]),
        ("ghost_experiences", experiences, budgets[2]),
    )
    return tuple(
        GhostContextSource(key=key, text=str(value or "").strip(), budget=budget, scope=scope)
        for key, value, budget in values
        if str(value or "").strip()
    )


def source_from_value(*, key: str, text: object, budget: int, scope: str,
                      capability_id: str = "local_context") -> GhostContextSource | None:
    value = str(text or "").strip()
    if not value:
        return None
    return GhostContextSource(
        key=key,
        text=value,
        budget=budget,
        scope=scope,
        capability_id=capability_id,
    )


__all__ = [
    "GhostAdmissionError",
    "GhostAdmissionRejection",
    "GhostAdmissionResult",
    "GhostContextSource",
    "admit_ghost_context",
    "request_scope",
    "source_from_value",
    "sources_from_values",
]
