"""Pure Safe Context Epoch projections over model-visible context facts.

A context epoch groups the context that entered one provider turn: every
model-visible section admitted at a safe provider-turn boundary shares one
content-addressed ``epoch id`` (the sha256 prefix of the exact outbound
bytes), so an auditor can reconstruct which sources were visible together.
The id identifies turn *content*, not a numbered provider call: identical
re-sends intentionally share the same epoch and stay deduplicated in the
trace, while any byte difference yields a new epoch.

This module owns nothing: it performs no I/O, never calls models, never holds
state between turns, and never imports runtime layers. It projects already
rendered sources into bounded admission records that contain digests, sizes,
budgets, and refs only. Sources without content or without a usable key fail
closed: they project to nothing instead of inventing refs.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass

EPOCH_REF_PREFIX = "ctx_epoch:"
SOURCE_REF_PREFIX = "context_source:"
PROVIDER_TURN_BOUNDARY = "provider_send"
PROVIDER_TURN_ADMISSION = "provider_turn_boundary"
MAX_SNAPSHOT_SOURCES = 64
MAX_ADMISSION_CHARS = 1_000_000


def context_epoch_id(value: object) -> str:
    """Return the stable content-addressed epoch ref for one outbound prompt."""
    text = str(value or "")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return f"{EPOCH_REF_PREFIX}{digest}"


def context_source_ref(key: object) -> str:
    """Return the stable source ref for one named context source key.

    Empty or unusable keys produce an empty ref so callers can skip the
    source instead of emitting an incomplete ``context_source:`` ref.
    """
    identifier = _identifier(key, 80)
    if not identifier or not identifier.strip("_"):
        return ""
    return SOURCE_REF_PREFIX + identifier


@dataclass(frozen=True)
class ContextAdmission:
    """One rendered context source admitted into a model-visible turn."""

    source_key: str
    source_ref: str
    capability_id: str = ""
    admission_reason: str = ""
    budget: int = 0
    chars: int = 0
    truncated: bool = False
    digest: str = ""

    def to_payload(self) -> dict[str, object]:
        capability = _identifier(self.capability_id, 80)
        reason = _identifier(self.admission_reason, 80)
        payload: dict[str, object] = {
            "source_key": _identifier(self.source_key, 80),
            "source_ref": _clip(self.source_ref, 120),
            "digest": self.digest,
            "chars": max(0, min(int(self.chars or 0), MAX_ADMISSION_CHARS)),
            "budget": max(0, int(self.budget or 0)),
            "truncated": bool(self.truncated),
        }
        if capability:
            payload["capability_id"] = capability
        if reason:
            payload["admission_reason"] = reason
        return payload


@dataclass(frozen=True)
class ContextEpoch:
    """One provider-turn boundary plus the sources admitted through it."""

    epoch_id: str
    boundary: str = PROVIDER_TURN_BOUNDARY
    admissions: tuple[ContextAdmission, ...] = ()

    def to_payload(self) -> dict[str, object]:
        return {
            "epoch_id": _identifier(self.epoch_id, 80),
            "boundary": _identifier(self.boundary, 40),
            "admissions": [item.to_payload() for item in self.admissions],
        }


@dataclass(frozen=True)
class ContextSnapshot:
    """Bounded read model of one epoch's admitted sources."""

    epoch_id: str
    admissions: tuple[ContextAdmission, ...] = ()

    def to_payload(self) -> dict[str, object]:
        return {
            "epoch_id": _identifier(self.epoch_id, 80),
            "admissions": [item.to_payload() for item in self.admissions],
        }


def admission_from_rendered_source(
    rendered_source: object,
    *,
    admission_reason: str = "",
) -> ContextAdmission | None:
    """Project one rendered context source into a bounded admission record.

    This is the single shared projection for "one source became part of a
    model-visible turn"; the snapshot builder and RunTrace's context-source
    rows both consume it so production and tests share one ref/digest
    vocabulary. Sources without text or without a usable key project to
    nothing (fail closed) instead of inventing refs.
    """
    text = str(getattr(rendered_source, "text", "") or "")
    if not text:
        return None
    key = getattr(rendered_source, "key", "")
    source_ref = context_source_ref(key)
    if not source_ref:
        return None
    reason = (
        str(getattr(rendered_source, "admission_reason", "") or "")
        or admission_reason
    )
    chars = len(text)
    truncated = bool(getattr(rendered_source, "truncated", False))
    if chars > MAX_ADMISSION_CHARS:
        # The audit row describes the outbound bytes as bound, not the
        # source: clamp the count to the budget cap and mark it, instead of
        # silently overstating.
        chars = MAX_ADMISSION_CHARS
        truncated = True
    return ContextAdmission(
        source_key=_identifier(key, 80),
        source_ref=source_ref,
        capability_id=_identifier(getattr(rendered_source, "capability_id", ""), 80),
        admission_reason=_identifier(reason, 80),
        budget=max(0, int(getattr(rendered_source, "budget", 0) or 0)),
        chars=chars,
        truncated=truncated,
        digest="sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def snapshot_from_rendered_sources(
    rendered_sources: Iterable[object],
    *,
    epoch_id: str,
    admission_reason: str = "",
) -> ContextSnapshot:
    """Project rendered context sources into a bounded admission snapshot."""
    admissions: list[ContextAdmission] = []
    for source in rendered_sources:
        admission = admission_from_rendered_source(
            source,
            admission_reason=admission_reason,
        )
        if admission is None:
            continue
        admissions.append(admission)
        if len(admissions) >= MAX_SNAPSHOT_SOURCES:
            break
    return ContextSnapshot(
        epoch_id=str(epoch_id or ""),
        admissions=tuple(admissions),
    )


def _clip(value: object, limit: int) -> str:
    text = str(value or "").strip()
    return text[:limit]


def _identifier(value: object, limit: int) -> str:
    text = _clip(value, limit)
    return "".join(char if char.isalnum() or char in "._:-" else "_" for char in text)
