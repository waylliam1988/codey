"""Bounded Ghost-to-Research topic continuity projection (Topic Planner v1).

A pure read model over already-audited local facts: structured research
interests, bounded Ghost continuity items, and prior evidence-ledger claim
refs. It projects them into one short model-visible hint text plus
digest-only metrics for the run trace.

Hard boundaries (roadmap 0.4.12):

- Continuity can relocate old refs and suggest what to re-check next.
  It cannot create facts or evidence: no output type carries evidence
  references, and every prior-claim ref is permanently stale.
- It cannot execute anything: no provider, browser, search, store, tool,
  or I/O of its own. It never imports the Ghost runtime; callers hand in
  bounded payloads or item-like objects and the projection stays local.
- Prompt framing must not leak internal vocabulary (Ghost, Work Queue,
  Concept Graph); unsafe or secret-bearing texts are dropped, fail open.

Topic Planner semantics: candidates only suggest what the next Research run
may investigate. Nothing here starts a run, searches, fetches, or writes.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Mapping

TOPIC_CONTINUITY_SCHEMA_VERSION = 1
_PROJECTION_KIND = "research_topic_continuity_projection"
CONTEXT_SOURCE_KEY = "research_topic_continuity"
PROMPT_SOURCE_REF = "local_context:research_topic_continuity"
DEFAULT_TOPIC_BUDGET_CHARS = 900
MAX_TOPIC_CANDIDATES = 4
MAX_TOPIC_CLAIM_REFS = 16
MAX_TOPIC_TEXT_CHARS = 160
MAX_TOPIC_REF_CHARS = 160
MAX_TOPIC_ITEM_REFS = 8
MAX_TOPIC_WARNINGS = 12

ITEM_KIND_OPEN_QUESTION = "open_question"
ITEM_KIND_PRIOR_CLAIM = "prior_claim"
ITEM_KIND_CORRECTION = "correction"
ITEM_KIND_PREFERENCE = "preference"

# Only these Ghost continuity kinds carry research-topic value; focus,
# goal, and project lines stay in the chat-side context instead of leaking
# into every research prompt.
_GHOST_ITEM_KIND_MAP = {
    "open_question": ITEM_KIND_OPEN_QUESTION,
    "fresh_correction": ITEM_KIND_CORRECTION,
    "recently_reinforced_preference": ITEM_KIND_PREFERENCE,
}

# Internal machinery names that must never reach model-visible continuity
# text, mirroring the knowledge-layer filter conventions. "memory" is
# deliberately absent: it is a common content word in research questions,
# so it is enforced on the framing lines (which never contain it) instead
# of being banned as item text.
_BANNED_TEXT_TERMS = ("ghost", "work queue", "concept graph")
# The framing/header vocabulary itself must avoid every internal name the
# roadmap calls out, including "memory".
_BANNED_FRAMING_TERMS = ("ghost", "memory", "work queue", "concept graph")
_SECRET_MARKERS = ("api_key", "api key", "password", "token sk-")

_HEADER = (
    "Local research continuity. This is not evidence.\n"
    "Treat every line below as a lead that may need re-checking; verify "
    "against opened sources before factual claims. Do not cite this section."
)


@dataclass(frozen=True)
class TopicContinuityItem:
    """One bounded continuity lead. Refs relocate history; they are not facts."""

    kind: str
    text: str
    refs: tuple[str, ...] = ()
    stale: bool = False
    reason_codes: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, object]:
        return {
            "refs": list(self.refs),
            "kind": self.kind,
            "stale": bool(self.stale),
            "reason_codes": list(self.reason_codes),
        }


@dataclass(frozen=True)
class TopicPlannerCandidate:
    """One suggested next-research question. A lead, never an answer."""

    candidate_id: str
    question: str
    source_refs: tuple[str, ...] = ()
    risk_codes: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "source_refs": list(self.source_refs),
            "risk_codes": list(self.risk_codes),
        }


@dataclass(frozen=True)
class TopicContinuityProjection:
    """Digest-only read model of one admission decision."""

    prompt_text: str
    items: tuple[TopicContinuityItem, ...] = ()
    candidates: tuple[TopicPlannerCandidate, ...] = ()
    claim_ref_count: int = 0
    warnings: tuple[str, ...] = ()
    truncated: bool = False

    @property
    def admitted(self) -> bool:
        return bool(self.prompt_text.strip())

    def to_payload(self) -> dict[str, object]:
        item_rows = [item.to_payload() for item in self.items]
        candidate_rows = [candidate.to_payload() for candidate in self.candidates]
        reason_codes = sorted({
            code
            for item in self.items
            for code in item.reason_codes
        })
        digest_source = json.dumps(
            {
                "kind": _PROJECTION_KIND,
                "items": item_rows,
                "candidates": candidate_rows,
                "claim_ref_count": max(0, int(self.claim_ref_count)),
                "truncated": bool(self.truncated),
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        return {
            "schema_version": TOPIC_CONTINUITY_SCHEMA_VERSION,
            "kind": _PROJECTION_KIND,
            "context_source": CONTEXT_SOURCE_KEY,
            "admitted": self.admitted,
            "item_count": len(self.items),
            "candidate_count": len(self.candidates),
            "claim_ref_count": max(0, int(self.claim_ref_count)),
            "truncated": bool(self.truncated),
            "reason_codes": reason_codes,
            "warnings": list(self.warnings),
            "items": item_rows,
            "candidates": candidate_rows,
            "digest": "sha256:" + hashlib.sha256(
                digest_source.encode("utf-8")
            ).hexdigest(),
        }


def project_topic_continuity(
    *,
    interest_hints: Iterable[Any] = (),
    continuity_hints: Iterable[Any] = (),
    claim_refs: Iterable[Any] = (),
    budget_chars: int = DEFAULT_TOPIC_BUDGET_CHARS,
) -> TopicContinuityProjection:
    """Project bounded local hints into one admitted-or-empty continuity."""
    warnings: list[str] = []
    items: list[TopicContinuityItem] = []

    interest_rows = tuple(interest_hints or ())
    continuity_rows = tuple(continuity_hints or ())
    claim_rows = tuple(claim_refs or ())

    for hint in interest_rows:
        item = _item_from_interest_hint(hint)
        if item is None:
            warnings.append("interest_hint_skipped")
            continue
        items.append(item)

    for hint in continuity_rows:
        item = _item_from_continuity_hint(hint)
        if item is not None:
            items.append(item)

    claim_items, claim_input_count = _items_from_claim_refs(claim_rows)
    items.extend(claim_items)

    items = _dedupe_items(items)
    candidates = build_topic_candidates(items)
    corrections = tuple(item for item in items if item.kind == ITEM_KIND_CORRECTION)
    preferences = tuple(item for item in items if item.kind == ITEM_KIND_PREFERENCE)
    prompt_text = render_topic_continuity(
        candidates=candidates,
        corrections=corrections,
        preferences=preferences,
        claim_ref_count=len(claim_items),
        budget_chars=budget_chars,
    )
    return TopicContinuityProjection(
        prompt_text=prompt_text,
        items=tuple(items),
        candidates=candidates,
        claim_ref_count=len(claim_items),
        warnings=_bounded_warnings(warnings),
        truncated=(
            claim_input_count > len(claim_items)
            or len([item for item in items if item.kind == ITEM_KIND_OPEN_QUESTION])
            > MAX_TOPIC_CANDIDATES
        ),
    )


def build_topic_candidates(
    items: Iterable[TopicContinuityItem],
    *,
    limit: int = MAX_TOPIC_CANDIDATES,
) -> tuple[TopicPlannerCandidate, ...]:
    """Deterministically deduplicate and bound open-question leads."""
    by_question: dict[str, list[TopicContinuityItem]] = {}
    order: list[str] = []
    for item in items or ():
        if item.kind != ITEM_KIND_OPEN_QUESTION:
            continue
        key = " ".join(item.text.split()).casefold()
        if not key:
            continue
        bucket = by_question.get(key)
        if bucket is None:
            bucket = []
            by_question[key] = bucket
            order.append(key)
        bucket.append(item)
    candidates: list[TopicPlannerCandidate] = []
    for key in order:
        refs: list[str] = []
        for item in by_question[key]:
            for ref in item.refs:
                if ref and ref not in refs:
                    refs.append(ref)
        candidates.append(TopicPlannerCandidate(
            candidate_id=_candidate_id(key),
            question=by_question[key][0].text,
            source_refs=tuple(refs[:MAX_TOPIC_ITEM_REFS]),
            risk_codes=(
                ("repeated_open_question",)
                if len(by_question[key]) > 1 or len(refs) > 1
                else ()
            ),
        ))
    ranked = sorted(
        candidates,
        key=lambda row: (
            -len(row.source_refs),
            len(row.risk_codes),
            row.question.casefold(),
            row.candidate_id,
        ),
    )
    return tuple(ranked[:max(0, int(limit))])


def render_topic_continuity(
    *,
    candidates: Iterable[TopicPlannerCandidate] = (),
    corrections: Iterable[TopicContinuityItem] = (),
    preferences: Iterable[TopicContinuityItem] = (),
    claim_ref_count: int = 0,
    budget_chars: int = DEFAULT_TOPIC_BUDGET_CHARS,
) -> str:
    """Render the bounded model-visible hint block within the char budget."""
    budget = max(0, int(budget_chars or 0))
    if budget <= 0:
        return ""
    sections: list[tuple[str, list[str]]] = []

    candidate_rows = [row for row in (candidates or ()) if row.question]
    if candidate_rows:
        sections.append((
            "Suggested next-research topics (questions, not answers):",
            [f"- {row.question}" for row in candidate_rows],
        ))

    correction_rows = [item for item in (corrections or ()) if item.text]
    if correction_rows:
        sections.append((
            "Earlier corrections that may affect this topic; re-check before use:",
            [f"- {item.text}" for item in correction_rows],
        ))

    preference_rows = [item for item in (preferences or ()) if item.text]
    if preference_rows:
        sections.append((
            "Framing preferences (style only, not facts):",
            [f"- {item.text}" for item in preference_rows],
        ))

    claims = max(0, int(claim_ref_count or 0))
    if claims:
        sections.append((
            "",
            [
                f"Earlier local runs recorded {claims} prior claim(s) on this "
                "topic as refs only; re-check any remembered conclusion "
                "against fresh sources."
            ],
        ))

    parts = [_HEADER]
    used = len(_HEADER)
    for title, lines in sections:
        # Account for the block separator, the optional title line, and each
        # packed hint line; anything that cannot fit whole is skipped so the
        # remaining priority-ordered sections still fit.
        local_used = used + (2 + len(title) + 1 if title else 2)
        kept: list[str] = []
        for line in lines:
            cost = len(line) + (1 if kept else 0)
            if local_used + cost > budget:
                continue
            kept.append(line)
            local_used += cost
        if not kept:
            continue
        parts.append(((title + "\n") if title else "") + "\n".join(kept))
        used = local_used
    if len(parts) <= 1:
        return ""
    rendered = "\n\n".join(parts)
    lowered_framing = "\n".join((
        _HEADER,
        *(title for title, _lines in sections),
    )).casefold()
    # Guard the invariant on our own framing vocabulary; item texts were
    # already screened at construction time by topic_item/_clean_text.
    if any(term in lowered_framing for term in _BANNED_FRAMING_TERMS):
        return ""
    return rendered


def topic_item(
    *,
    kind: str,
    ref: str,
    text: object,
    stale: bool = False,
    reason_codes: Iterable[str] = (),
) -> TopicContinuityItem | None:
    """Construct one cleaned, safety-screened continuity item (or None)."""
    clean_kind = str(kind or "").strip().lower()
    clean_text = _clean_text(text, MAX_TOPIC_TEXT_CHARS)
    clean_ref = _clean_token(ref, MAX_TOPIC_REF_CHARS)
    if not clean_kind or not clean_ref or not clean_text:
        return None
    codes = tuple(
        code
        for code in (_clean_token(value, 80) for value in reason_codes or ())
        if code
    )
    return TopicContinuityItem(
        kind=clean_kind,
        text=clean_text,
        refs=(clean_ref,),
        stale=bool(stale),
        reason_codes=codes,
    )


def _candidate_id(normalized_question: str) -> str:
    digest = hashlib.sha256(
        normalized_question.encode("utf-8", errors="replace")
    ).hexdigest()[:16]
    return f"topic_{digest}"


def _item_from_interest_hint(hint: Any) -> TopicContinuityItem | None:
    identifier = _clean_token(_field(hint, "id"), 120)
    ref = _clean_token(_field(hint, "ref"), MAX_TOPIC_REF_CHARS) or (
        f"research_interest:{identifier}" if identifier else ""
    )
    return topic_item(
        kind=ITEM_KIND_OPEN_QUESTION,
        ref=ref,
        text=_field(hint, "question"),
        reason_codes=("interest_lead",),
    )


def _item_from_continuity_hint(hint: Any) -> TopicContinuityItem | None:
    mapped = _GHOST_ITEM_KIND_MAP.get(str(_field(hint, "kind") or ""))
    if mapped is None:
        return None
    identifier = _clean_token(_field(hint, "id"), 120)
    if not identifier:
        return None
    stale = mapped == ITEM_KIND_CORRECTION
    codes = ("ghost_correction_needs_recheck",) if stale else ()
    return topic_item(
        kind=mapped,
        ref=f"continuity:{identifier}",
        text=_field(hint, "text"),
        stale=stale,
        reason_codes=codes,
    )


def _items_from_claim_refs(
    claim_refs: Iterable[Any],
) -> tuple[tuple[TopicContinuityItem, ...], int]:
    """Project raw claim refs into permanently-stale prior-claim items.

    The returned count is the number of usable inputs seen (before the cap),
    so callers can report truncation honestly.
    """
    items: list[TopicContinuityItem] = []
    input_count = 0
    for value in claim_refs or ():
        raw = value
        if isinstance(raw, Mapping):
            raw = raw.get("ref") or raw.get("claim_ref") or ""
        text = str(raw or "").strip()
        if not text:
            continue
        input_count += 1
        if len(items) >= MAX_TOPIC_CLAIM_REFS:
            continue
        ref = text if text.startswith("prior_claim:") else f"prior_claim:{text}"
        item = topic_item(
            kind=ITEM_KIND_PRIOR_CLAIM,
            ref=ref,
            text=ref,
            stale=True,
            reason_codes=("prior_claim_needs_recheck",),
        )
        if item is not None:
            items.append(item)
    return tuple(items), input_count


def _dedupe_items(items: Iterable[TopicContinuityItem]) -> tuple[TopicContinuityItem, ...]:
    """Merge same-kind same-text items, unioning refs and reason codes."""
    order: list[tuple[str, str]] = []
    by_key: dict[tuple[str, str], TopicContinuityItem] = {}
    for item in items:
        key = (item.kind, " ".join(item.text.split()).casefold())
        current = by_key.get(key)
        if current is None:
            by_key[key] = item
            order.append(key)
            continue
        refs = list(current.refs)
        for ref in item.refs:
            if ref and ref not in refs:
                refs.append(ref)
        by_key[key] = TopicContinuityItem(
            kind=current.kind,
            text=current.text,
            refs=tuple(refs[:MAX_TOPIC_ITEM_REFS]),
            stale=current.stale or item.stale,
            reason_codes=tuple(dict.fromkeys((*current.reason_codes, *item.reason_codes))),
        )
    return tuple(by_key[key] for key in order)


def _bounded_warnings(warnings: Iterable[str]) -> tuple[str, ...]:
    out: list[str] = []
    for warning in warnings or ():
        text = str(warning or "").strip()
        if text and text not in out:
            out.append(text)
        if len(out) >= MAX_TOPIC_WARNINGS:
            break
    return tuple(out)


def _clean_text(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    lower = text.casefold()
    if not text:
        return ""
    if any(term in lower for term in _BANNED_TEXT_TERMS):
        return ""
    if any(marker in lower for marker in _SECRET_MARKERS):
        return ""
    if len(text) > limit:
        text = text[:limit].rstrip()
    return text


def _clean_token(value: object, limit: int) -> str:
    text = str(value or "").strip()
    if any(char in text for char in "\r\n\t"):
        return ""
    if len(text) > limit:
        text = text[:limit].rstrip()
    return text


def _field(value: Any, key: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(key)
    return getattr(value, key, "")


__all__ = [
    "CONTEXT_SOURCE_KEY",
    "DEFAULT_TOPIC_BUDGET_CHARS",
    "ITEM_KIND_CORRECTION",
    "ITEM_KIND_OPEN_QUESTION",
    "ITEM_KIND_PREFERENCE",
    "ITEM_KIND_PRIOR_CLAIM",
    "MAX_TOPIC_CANDIDATES",
    "MAX_TOPIC_CLAIM_REFS",
    "PROMPT_SOURCE_REF",
    "TOPIC_CONTINUITY_SCHEMA_VERSION",
    "TopicContinuityItem",
    "TopicContinuityProjection",
    "TopicPlannerCandidate",
    "build_topic_candidates",
    "project_topic_continuity",
    "render_topic_continuity",
    "topic_item",
]
