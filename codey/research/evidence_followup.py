"""Deprecated shim: use ``codey.research.evidence_rules`` + ``codey.operations.evidence_followup``.

The old direct ``provider.send`` loop (``run_evidence_followup``,
``_evaluate_followup_reply``, ``_FollowupAttempt``) was removed. Production
uses the shared kernel path; rules (Controller/prompts/result) live in
``evidence_rules``.
"""

from __future__ import annotations

from codey.research.evidence_rules import (
    EvidenceFollowupController,
    EvidenceFollowupResult,
    build_evidence_followup_prompt,
    build_evidence_followup_repair_prompt,
)

__all__ = [
    "EvidenceFollowupController",
    "EvidenceFollowupResult",
    "build_evidence_followup_prompt",
    "build_evidence_followup_repair_prompt",
]
