"""Trace schema constants: versions, bounds, and field vocabularies.

Single source for every numeric bound the Trace sidecar enforces. Values
are byte-identical to the former ``trace.py`` header; only the home
changed. ``trace.py``, ``details.py`` and ``project_completion_flow.py``
import from here. This module has no behavior and no codey imports other
than the completion-checks bound it derives from.
"""

from __future__ import annotations

from codey.completion.contract import (
    MAX_COMPLETION_CHECKS as _MAX_COMPLETION_CHECKS,
)

SCHEMA_VERSION = 1
TRACE_KIND = "run_trace_manifest"
MAX_TRACE_BYTES = 256 * 1024
MAX_TEXT_CHARS = 240
MAX_PROMPT_SECTIONS = 80
MAX_REFS = 64
MAX_FALLBACKS = 16
MAX_FAILURES = 16
MAX_WARNINGS = 16
MAX_PERMISSION_PROFILES = 16
MAX_TOOL_CONTRACTS = 16
MAX_POLICY_DECISIONS = 80
MAX_RESEARCH_RECORDS = 8
MAX_EVIDENCE_LEDGER_WRITES = 8
MAX_RESEARCH_PROOF_REVIEWS = 8
MAX_RESEARCH_PLANS = 8
MAX_RESEARCH_PIPELINE_RUNS = 8
MAX_RESEARCH_CONNECTOR_ERRORS = 8
MAX_RESEARCH_DONE_COMPILATIONS = 8
MAX_ANALYSIS_RUNS = 8
MAX_ARTIFACT_REFS = 16
MAX_REPRODUCIBILITY_CAPSULES = 8
MAX_CAPSULE_ARTIFACT_REFS = 8
MAX_REVIEW_FINDINGS = 16
MAX_PLANNER_GAPS = 16
MAX_GAP_FINDING_REFS = 4
MAX_COMPLETION_PROOFS = 8
MAX_COMPLETION_CHECK_ROWS = _MAX_COMPLETION_CHECKS
MAX_EDIT_INTEGRITY_ROWS = 8
MAX_SOURCE_TRUST_ROWS = 32
MAX_SOURCE_TRUST_CLASSES = 3
MAX_BRIEF_PROJECTIONS = 8
MAX_BRIEF_CLAIM_ROWS = 16
MAX_BRIEF_REFS = 24
MAX_TOPIC_CONTINUITY_ROWS = 8
MAX_COMPLETION_REPAIR_ROWS = 4
MAX_PROTOCOL_ERROR_KINDS = 16
MAX_PROTOCOL_UNKNOWN_TOOLS = 8
MAX_PROTOCOL_VALID_TURNS = 64
MAX_PROMPT_SURFACES = 80
CHECKPOINT_FLUSH_INTERVAL = 8
REVIEW_FINDING_REF_KINDS: dict[str, str] = {
    "claim_ref": "claim",
    "evidence_ref": "evidence",
    "source_ref": "source",
    "analysis_run_ref": "analysis_run",
    "artifact_ref": "artifact_version",
    "proof_ref": "research_proof",
}
RESEARCH_ANSWER_STATUSES = frozenset({
    "answered",
    "partial",
    "insufficient_evidence",
    "not_answered",
})
