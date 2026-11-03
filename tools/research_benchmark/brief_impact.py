"""Offline implementation-impact contracts and brief handoff rendering.

Used by benchmark scoring and archived/manual replay, outside the production
package. Unsupported claims remain risks and never authorize implementation.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from codey.research.brief_projection import (
    ANSWER_STATUSES,
    MAX_CLAIM_TEXT_CHARS,
    MAX_ITEM_TEXT_CHARS,
    ClaimSummary,
    ResearchBriefProjection,
    _text_items,
)
from codey.research.guards import status_token as _status_token
from codey.utils.refs import clip as _clip
from codey.utils.refs import identifier as _identifier
from codey.utils.text_budget import clip_middle

MAX_HANDOFF_CHARS = 6000
MAX_IMPACT_FILES = 12
MAX_IMPACT_ITEMS = 8
CONSTRAINT_SUPPORTS = frozenset({"verified", "assumption_risk"})
_PATH_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+ -]*(/[A-Za-z0-9._@+ -]*)*$")


@dataclass(frozen=True)
class ImplementationConstraint:
    """One implementation constraint backed by explicit research support."""

    text: str
    support: str = "verified"
    claim_refs: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, object]:
        return {
            "text": _clip(self.text, MAX_ITEM_TEXT_CHARS),
            "support": _support_token(self.support),
            "claim_refs": list(self.claim_refs[:4]),
        }


@dataclass(frozen=True)
class ResearchImpactContract:
    affected_files: tuple[str, ...] = ()
    implementation_constraints: tuple[ImplementationConstraint, ...] = ()
    test_suggestions: tuple[str, ...] = ()
    risk_notes: tuple[str, ...] = ()
    out_of_scope_items: tuple[str, ...] = ()
    decision_refs: tuple[str, ...] = ()

    def is_empty(self) -> bool:
        return not (
            self.affected_files
            or self.implementation_constraints
            or self.test_suggestions
            or self.risk_notes
            or self.out_of_scope_items
            or self.decision_refs
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "affected_files": list(self.affected_files),
            "implementation_constraints": [
                item.to_payload() for item in self.implementation_constraints
            ],
            "test_suggestions": list(self.test_suggestions),
            "risk_notes": list(self.risk_notes),
            "out_of_scope_items": list(self.out_of_scope_items),
            "decision_refs": list(self.decision_refs),
        }


def constraints_from_claims(claims: Iterable[object]) -> tuple[ImplementationConstraint, ...]:
    """Verified-support constraints derivable from brief claims."""

    out: list[ImplementationConstraint] = []
    for item in claims or ():
        if not isinstance(item, ClaimSummary):
            continue
        if item.status != "evidence_backed" or not item.claim_ref:
            continue
        out.append(ImplementationConstraint(
            text=item.text,
            support="verified",
            claim_refs=(item.claim_ref,),
        ))
        if len(out) >= MAX_IMPACT_ITEMS:
            break
    return tuple(out)


def build_impact_contract(
    *,
    claims: Iterable[object] = (),
    affected_files: Iterable[object] = (),
    test_suggestions: Iterable[object] = (),
    risk_notes: Iterable[object] = (),
    out_of_scope_items: Iterable[object] = (),
    decision_refs: Iterable[object] = (),
) -> ResearchImpactContract | None:
    """Build the impact contract, enforcing the support boundary.

    ``claims`` feed verified constraints automatically; assumption-backed or
    unsupported claims are demoted into ``risk_notes``. Explicitly passed
    files/tests/risks stay bounded context only -- nothing here authorizes a
    tool or changes permissions.
    """

    summaries = [
        item for item in claims or () if isinstance(item, ClaimSummary)
    ]
    constraints = list(constraints_from_claims(summaries))
    risks = _text_items(risk_notes, MAX_IMPACT_ITEMS, MAX_ITEM_TEXT_CHARS)
    for item in summaries:
        if item.status == "unsupported":
            entry = f"{item.text} [unsupported_claim]"
            if entry not in risks and len(risks) < MAX_IMPACT_ITEMS:
                risks = (*risks, entry)
        elif item.status == "assumption":
            entry = f"{item.text} [declared_assumption]"
            if entry not in risks and len(risks) < MAX_IMPACT_ITEMS:
                risks = (*risks, entry)
    contract = ResearchImpactContract(
        affected_files=_file_tokens(affected_files),
        implementation_constraints=tuple(constraints),
        test_suggestions=_text_items(test_suggestions, MAX_IMPACT_ITEMS, MAX_ITEM_TEXT_CHARS),
        risk_notes=risks,
        out_of_scope_items=_text_items(out_of_scope_items, MAX_IMPACT_ITEMS, MAX_ITEM_TEXT_CHARS),
        decision_refs=_bounded_ids(decision_refs, 8),
    )
    if contract.is_empty():
        return None
    return contract


def render_handoff(
    projection: ResearchBriefProjection,
    impact: ResearchImpactContract | None = None,
) -> str:
    """Render the short model-visible handoff for one projection."""

    lines: list[str] = []
    status = _status_token(projection.answer_status, ANSWER_STATUSES, default="not_answered")
    counts = projection.counts()
    summary = (
        f"- record: {projection.record_ref}"
        + (f" ({status})" if status != "not_answered" else "")
        + f"; evidence refs: {counts['evidence']} across {counts['sources']} sources"
    )
    lines.append(summary)
    supported = projection.supported_claims()
    if supported:
        lines.append("Concluded (verified support):")
        lines.extend(f"- {_render_claim(item)}" for item in supported)
    uncertain = projection.uncertain_claims()
    if uncertain:
        lines.append("Uncertain / assumptions:")
        lines.extend(f"- {_render_claim(item)}" for item in uncertain)
    if projection.open_questions:
        lines.append("Open questions:")
        lines.extend(f"- {item}" for item in projection.open_questions)
    if projection.warnings:
        lines.append("Evidence caveats: " + ", ".join(projection.warnings))
    if impact is not None and not impact.is_empty():
        lines.extend(_impact_lines(impact))
    rendered, _truncated = clip_middle("\n".join(lines), MAX_HANDOFF_CHARS)
    return rendered.strip()


def _impact_lines(impact: ResearchImpactContract) -> list[str]:
    lines = ["Implementation impact (research-derived, context only):"]
    if impact.affected_files:
        lines.append("- files likely affected: " + ", ".join(impact.affected_files))
    for constraint in impact.implementation_constraints:
        tag = "verified" if constraint.support == "verified" else "assumption+risk"
        lines.append(f"- constraint [{tag}]: {constraint.text}")
    for risk in impact.risk_notes:
        lines.append(f"- risk: {risk}")
    if impact.test_suggestions:
        lines.append("- tests to consider (not authorized by this handoff):")
        lines.extend(f"  - {item}" for item in impact.test_suggestions)
    if impact.out_of_scope_items:
        lines.append("- out of scope: " + "; ".join(impact.out_of_scope_items))
    return lines


def _render_claim(item: ClaimSummary) -> str:
    suffix = f" [{item.claim_ref}]" if item.claim_ref else ""
    if item.status == "unsupported":
        suffix += " [unsupported]"
    elif item.status == "assumption":
        suffix += " [assumption]"
    return f"{_clip(item.text, MAX_CLAIM_TEXT_CHARS)}{suffix}"


def _bounded_ids(values: Iterable[object], limit: int) -> tuple[str, ...]:
    out: list[str] = []
    for value in values or ():
        token = _identifier(value, 120)
        if token and token not in out:
            out.append(token)
        if len(out) >= max(0, limit):
            break
    return tuple(out)


def _file_tokens(values: Iterable[object]) -> tuple[str, ...]:
    out: list[str] = []
    for value in values or ():
        text = str(value or "").strip().replace("\\", "/")
        if not text or len(text) > 200:
            continue
        if text.startswith(("/", "~")) or ".." in text or ":" in text:
            continue
        if not _PATH_TOKEN_RE.match(text):
            continue
        normalized = text.strip("./")
        if normalized and normalized not in out:
            out.append(normalized)
        if len(out) >= MAX_IMPACT_FILES:
            break
    return tuple(out)


def _support_token(value: object) -> str:
    return _status_token(value, CONSTRAINT_SUPPORTS, default="verified", limit=20)
