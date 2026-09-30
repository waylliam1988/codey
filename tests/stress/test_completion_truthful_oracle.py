"""Completion truthfulness oracle: completed implies facts, not self-report.

Locks that the shared stress oracle rejects completions whose proof is
missing, whose verification identity does not bind the current workspace
version, or whose strict-Research citations escape the evidence ledger.
Views carry test-side facts (real log/workspace/ledger reads), never the
verdict's own claims.
"""
from __future__ import annotations

import pytest

from tests.stress.oracle import InvariantChecker, InvariantViolation


def _valid_view() -> dict:
    return {
        "completed": True,
        "proof_exists": True,
        "required_checks_passed": True,
        "verification_required": True,
        "verification_identity_valid": True,
        "verification_revision": 7,
        "workspace_revision": 7,
        "verification_fingerprint": "sha256:" + "a" * 64,
        "workspace_fingerprint": "sha256:" + "a" * 64,
        "strict_research": True,
        "ledger_valid": True,
        "opened_sources": ["https://example.com/a"],
        "cited_sources": ["https://example.com/a"],
        "cited_evidence_sources": ["https://example.com/a"],
        "report_valid": True,
    }


def test_incomplete_task_needs_no_proof() -> None:
    view = _valid_view()
    view["completed"] = False
    view["proof_exists"] = False
    InvariantChecker().check_completion_truthful(view)


def test_completed_without_proof_fails() -> None:
    view = _valid_view()
    view["proof_exists"] = False
    with pytest.raises(InvariantViolation):
        InvariantChecker().check_completion_truthful(view)


def test_stale_verification_revision_fails() -> None:
    view = _valid_view()
    view["verification_revision"] = 2
    with pytest.raises(InvariantViolation):
        InvariantChecker().check_completion_truthful(view)


def test_mismatched_verification_fingerprint_fails() -> None:
    view = _valid_view()
    view["verification_fingerprint"] = "sha256:" + "b" * 64
    with pytest.raises(InvariantViolation):
        InvariantChecker().check_completion_truthful(view)


def test_strict_research_without_valid_ledger_fails() -> None:
    view = _valid_view()
    view["ledger_valid"] = False
    with pytest.raises(InvariantViolation):
        InvariantChecker().check_completion_truthful(view)


def test_citation_outside_opened_sources_fails() -> None:
    view = _valid_view()
    view["cited_sources"] = ["https://unopened.example/b"]
    with pytest.raises(InvariantViolation):
        InvariantChecker().check_completion_truthful(view)


def test_evidence_outside_opened_sources_fails() -> None:
    view = _valid_view()
    view["cited_evidence_sources"] = ["https://unopened.example/b"]
    with pytest.raises(InvariantViolation):
        InvariantChecker().check_completion_truthful(view)


def test_invalid_report_fails() -> None:
    view = _valid_view()
    view["report_valid"] = False
    with pytest.raises(InvariantViolation):
        InvariantChecker().check_completion_truthful(view)


def test_truthful_completion_passes() -> None:
    InvariantChecker().check_completion_truthful(_valid_view())
