"""Minimal Artifact lineage metadata for local run outputs.

v1 only projects Managed Output handles into stable artifact/version refs.
No artifact manager, no storage changes, no UI: managed_outputs.py remains the
only writer and size boundary. ``mime`` is pinned to ``text/plain`` because
Managed Outputs are stored as UTF-8 text in v1.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Mapping

from codey.research.identity import clip, stable_ref

ARTIFACT_REF_PREFIX = "artifact:"
ARTIFACT_VERSION_REF_PREFIX = "artifact_version:"
ARTIFACT_KIND_MANAGED_OUTPUT = "managed_output"
ARTIFACT_MIME_TEXT = "text/plain"
MAX_DERIVED_REFS = 8
MAX_SIZE_BYTES = 10**12

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_DERIVED_PREFIXES = (
    "source:",
    "evidence:",
    "analysis_run:",
    "run:",
)


@dataclass(frozen=True)
class ArtifactRef:
    artifact_id: str
    version_id: str
    artifact_kind: str
    sha256: str
    size: int
    mime: str
    origin_run_id: str
    produced_by: str
    stored_truncated: bool
    derived_from: tuple[str, ...]
    warnings: tuple[str, ...]

    def to_payload(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "version_id": self.version_id,
            "artifact_kind": self.artifact_kind,
            "sha256": self.sha256,
            "size": self.size,
            "mime": self.mime,
            "origin_run_id": self.origin_run_id,
            "produced_by": self.produced_by,
            "stored_truncated": self.stored_truncated,
            "derived_from": list(self.derived_from),
            "warnings": list(self.warnings),
        }


def is_valid_derived_ref(value: object) -> bool:
    """Derived refs may only point at Source/Evidence/AnalysisRun/Run facts."""

    text = str(value or "").strip()
    return text.startswith(_ALLOWED_DERIVED_PREFIXES) and len(text) <= 120


def _clean_sha256(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if _SHA256_RE.fullmatch(text) else ""


def _bounded_size(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(parsed, MAX_SIZE_BYTES))


def artifact_ref_from_managed_output(data: Mapping[str, object]) -> ArtifactRef | None:
    """Project one Managed Output audit payload into a lineage ArtifactRef.

    Returns None when the payload lacks a content digest, so malformed handles
    fail open instead of producing unversioned lineage entries.
    """

    if not isinstance(data, Mapping):
        return None
    handle = str(data.get("handle") or "").strip()
    sha256 = _clean_sha256(data.get("sha256"))
    if not handle or not sha256:
        return None
    warnings: list[str] = []
    if bool(data.get("stored_truncated")):
        # Truncation is part of capture quality, not an error, but lineage
        # consumers should know the bytes are not the full stream.
        warnings.append("stored_output_truncated")
    derived_raw = data.get("derived_from")
    derived: list[str] = []
    for item in (derived_raw if isinstance(derived_raw, Iterable) else ()):  # type: ignore[arg-type]
        ref = str(item or "").strip()
        if is_valid_derived_ref(ref):
            derived.append(ref)
        if len(derived) >= MAX_DERIVED_REFS:
            break
    return ArtifactRef(
        artifact_id=stable_ref(ARTIFACT_REF_PREFIX.removesuffix(":"), sha256),
        version_id=stable_ref(
            ARTIFACT_VERSION_REF_PREFIX.removesuffix(":"),
            sha256,
            handle,
        ),
        artifact_kind=ARTIFACT_KIND_MANAGED_OUTPUT,
        sha256=sha256,
        size=_bounded_size(data.get("stored_bytes")),
        mime=ARTIFACT_MIME_TEXT,
        origin_run_id=clip(data.get("origin_run_id"), 120),
        produced_by=clip(data.get("produced_by"), 120),
        stored_truncated=bool(data.get("stored_truncated")),
        derived_from=tuple(derived),
        warnings=tuple(warnings[:4]),
    )


__all__ = [
    "ARTIFACT_KIND_MANAGED_OUTPUT",
    "ARTIFACT_MIME_TEXT",
    "ArtifactRef",
    "artifact_ref_from_managed_output",
    "is_valid_derived_ref",
]
