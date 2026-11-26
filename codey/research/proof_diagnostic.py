from __future__ import annotations

from dataclasses import dataclass

from codey.research.evidence_runtime import normalize_runtime_ref as _normalize_runtime_ref
from codey.utils.refs import identifier

DIAGNOSTIC_REF_KINDS = (
    ("claim_ref", "claim"),
    ("evidence_ref", "evidence"),
    ("source_ref", "source"),
    ("relation_ref", "relation"),
)


@dataclass(frozen=True)
class ProofDiagnostic:
    """One located proof problem with the refs needed to act on it.

    Reason codes stay the single source of truth for meaning; the extra fields
    only record where the problem was observed, so downstream projections can
    point at the exact claim/evidence/source/relation instead of re-walking
    the relation graph.
    """

    reason_code: str
    claim_ref: str = ""
    evidence_ref: str = ""
    source_ref: str = ""
    relation_ref: str = ""

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "reason_code": identifier(self.reason_code, 80),
        }
        for key, ref_kind in DIAGNOSTIC_REF_KINDS:
            value = _normalize_runtime_ref(getattr(self, key), kind=ref_kind)
            if value:
                payload[key] = value
        return payload

    def _key(self) -> tuple[str, str, str, str, str]:
        return (
            self.reason_code,
            self.claim_ref,
            self.evidence_ref,
            self.source_ref,
            self.relation_ref,
        )
