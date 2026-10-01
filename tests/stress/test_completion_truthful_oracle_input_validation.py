"""Oracle input validation: missing or mistyped fields never pass as truthful.

Locks that ``check_completion_truthful`` rejects incomplete views instead of
treating absent information as success: each required field is required, bool
``True`` never stands in for an int revision, ``None == None`` never counts
as a fingerprint match, and strict-Research without real source sets fails.
"""
from __future__ import annotations

import pytest

from tests.stress.oracle import InvariantChecker, InvariantViolation


def _valid_view() -> dict:
    fp = "sha256:" + "a" * 64
    return {
        "completed": True,
        "proof_exists": True,
        "proof_checks": [{"check_id": "relevant_verification", "status": "pass"}],
        "required_checks_passed": True,
        "verification_required": True,
        "verification_observations": [{
            "command": "python -m pytest",
            "cwd": ".",
            "passed": True,
            "exit_code": 0,
            "revision": 1,
            "workspace_revision": 7,
            "workspace_fingerprint": fp,
        }],
        "verification_identity_valid": True,
        "verification_revision": 7,
        "workspace_revision": 7,
        "verification_fingerprint": fp,
        "workspace_fingerprint": fp,
        "strict_research": True,
        "ledger_valid": True,
        "opened_sources": ["https://example.com/a"],
        "cited_sources": ["https://example.com/a"],
        "cited_evidence_sources": ["https://example.com/a"],
        "report_valid": True,
        "report_text": "## 结论\n- ok [1]\n\n## 来源\n[1] Title - https://example.com/a",
    }


def test_missing_proof_field_fails() -> None:
    view = {"completed": True, "proof_exists": True}
    view.pop("proof_exists")
    with pytest.raises(InvariantViolation):
        InvariantChecker().check_completion_truthful(view)


def test_minimal_completed_view_without_verification_fields_fails() -> None:
    with pytest.raises(InvariantViolation):
        InvariantChecker().check_completion_truthful(
            {"completed": True, "proof_exists": True}
        )


def test_missing_required_checks_field_fails() -> None:
    view = _valid_view()
    view.pop("required_checks_passed")
    with pytest.raises(InvariantViolation):
        InvariantChecker().check_completion_truthful(view)


def test_bool_true_is_not_revision_one() -> None:
    view = _valid_view()
    view["verification_revision"] = True
    view["workspace_revision"] = 1
    with pytest.raises(InvariantViolation):
        InvariantChecker().check_completion_truthful(view)


def test_string_revision_is_not_int_revision() -> None:
    view = _valid_view()
    view["verification_revision"] = "7"
    view["workspace_revision"] = 7
    with pytest.raises(InvariantViolation):
        InvariantChecker().check_completion_truthful(view)


def test_missing_fingerprints_on_both_sides_fail() -> None:
    view = _valid_view()
    view.pop("verification_fingerprint")
    view.pop("workspace_fingerprint")
    with pytest.raises(InvariantViolation):
        InvariantChecker().check_completion_truthful(view)


def test_missing_verification_identity_flag_fails() -> None:
    view = _valid_view()
    view.pop("verification_identity_valid")
    with pytest.raises(InvariantViolation):
        InvariantChecker().check_completion_truthful(view)


def test_strict_research_without_source_sets_fails() -> None:
    view = {
        "completed": True,
        "proof_exists": True,
        "required_checks_passed": True,
        "strict_research": True,
        "ledger_valid": True,
        "report_valid": True,
    }
    with pytest.raises(InvariantViolation):
        InvariantChecker().check_completion_truthful(view)


def test_missing_ledger_valid_fails_for_strict_research() -> None:
    view = _valid_view()
    view.pop("ledger_valid")
    with pytest.raises(InvariantViolation):
        InvariantChecker().check_completion_truthful(view)


def test_missing_report_valid_fails_for_strict_research() -> None:
    view = _valid_view()
    view.pop("report_valid")
    with pytest.raises(InvariantViolation):
        InvariantChecker().check_completion_truthful(view)


def test_completed_without_task_requirements_fails() -> None:
    view = _valid_view()
    view.pop("verification_required")
    view.pop("strict_research")
    view.pop("required_checks_passed")
    with pytest.raises(InvariantViolation):
        InvariantChecker().check_completion_truthful(view)
