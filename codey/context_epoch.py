"""Pure Safe Context Epoch projections over model-visible context facts.

A context epoch groups the context that entered one provider turn: every
model-visible section admitted at a safe provider-turn boundary shares one
content-addressed ``epoch id``, so an auditor can reconstruct exactly which
sources were visible together without storing any of their bodies.

This module owns nothing: it performs no I/O, never calls models, never holds
state between turns, and never imports runtime layers. It projects already
rendered sources into bounded admission records that contain digests, sizes,
budgets, and refs only.
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
    """Return a stable content-addressed epoch ref for one outbound prompt."""
    text = str(value or "")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return f"{EPOCH_REF_PREFIX}{digest}"


def context_source_ref(key: object) -> str:
    """Return the stable source ref for one named context source key."""
    return SOURCE_REF_PREFIX + _identifier(key, 80)


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


def snapshot_from_rendered_sources(
    rendered_sources: Iterable[object],
    *,
    epoch_id: str,
    admission_reason: str = "",
) -> ContextSnapshot:
    """Project rendered context sources into a bounded admission snapshot.

    Rendered sources are duck-typed (key/text/budget/truncated/freshness plus
    optional capability_id/admission_reason) so this stays decoupled from the
    concrete dataclass while rejecting anything without content.
    """
    admissions: list[ContextAdmission] = []
    for source in rendered_sources:
        text = str(getattr(source, "text", "") or "")
        if not text:
            continue
        key = getattr(source, "key", "") or "context_source"
        reason = (
            str(getattr(source, "admission_reason", "") or "")
            or admission_reason
        )
        admissions.append(ContextAdmission(
            source_key=_identifier(key, 80),
            source_ref=context_source_ref(key),
            capability_id=_identifier(getattr(source, "capability_id", ""), 80),
            admission_reason=_identifier(reason, 80),
            budget=max(0, int(getattr(source, "budget", 0) or 0)),
            chars=len(text),
            truncated=bool(getattr(source, "truncated", False)),
            digest="sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest(),
        ))
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
