"""Loader failure must preserve the known requirement and block completion.

Same fact: required pytest, loader raises OSError, only compileall succeeds.
Both paths must block, not fall back to 'latest run is the requirement'.
A legal empty return is distinct from a load failure.
"""
from __future__ import annotations

import pytest

from codey.completion.verification_policy import VerificationCandidate
from codey.operations.completion_gate import evaluate
from codey.operations.project_verification import refresh_verification_candidates
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.runtime.observe.execution_evidence import ExecutionEvidence

FP = "sha256:" + "b" * 64


def make_session():
    session = TaskSession(
        policy=TaskPolicy(grants=frozenset({"control", "project.read", "project.write", "project.verify"})),
        task_kind="project",
        project="",
        max_turns=3,
    )
    session.edited_files = {"a.py": 1}
    session.set_workspace_state(1, FP)
    pytest_req = VerificationCandidate("python -m pytest", ".", "project")
    session.verification_candidates = (pytest_req,)
    session.verification_candidates_epoch = -1
    from codey.completion.verification_policy import select_verification_candidate

    session.selected_verification = select_verification_candidate(
        session.verification_candidates, tuple(session.edited_files)
    )
    assert session.selected_verification is not None
    return session


def _context(session):
    return {
        "execution_evidence": ExecutionEvidence(
            workspace_revision=session.workspace_revision,
            workspace_fingerprint=session.workspace_fingerprint,
        ),
        "project": "",
        "scope_files": ("a.py",),
        "run_id": "refresh-fail",
        "task": "fix a.py",
    }


@pytest.mark.parametrize("with_evidence", [False, True])
def test_loader_oserror_preserves_requirement_and_blocks(with_evidence):
    session = make_session()

    def boom():
        raise OSError("disk gone")

    session.verification_candidate_loader = boom
    refresh_verification_candidates(session)
    # Requirement preserved, failure recorded, epoch not marked successful.
    assert session.selected_verification is not None
    assert session.selected_verification.command == "python -m pytest"
    assert getattr(session, "verification_candidates_refresh_failed", False) is True
    session.record_verification(
        "python -m compileall .", 1, True, exit_code=0,
        workspace_revision=1, workspace_fingerprint=FP, cwd=".",
    )
    context = _context(session) if with_evidence else None
    assert evaluate(session, "done", context=context).complete is False


def test_loader_illegal_candidate_blocks():
    session = make_session()
    session.verification_candidate_loader = lambda: ("not-a-candidate",)
    refresh_verification_candidates(session)
    assert getattr(session, "verification_candidates_refresh_failed", False) is True
    assert session.selected_verification is not None
    session.record_verification(
        "python -m pytest", 1, True, exit_code=0,
        workspace_revision=1, workspace_fingerprint=FP, cwd=".",
    )
    # Even the nominally correct command must block while the refresh is
    # in a failed state: the requirement set is not trustworthy.
    assert evaluate(session, "done", context=None).complete is False
    assert evaluate(session, "done", context=_context(session)).complete is False


def test_recovery_after_loader_restores_success():
    session = make_session()

    def boom():
        raise OSError("disk gone")

    session.verification_candidate_loader = boom
    refresh_verification_candidates(session)
    assert getattr(session, "verification_candidates_refresh_failed", False) is True
    req = VerificationCandidate("python -m pytest", ".", "project")
    session.verification_candidate_loader = lambda: (req,)
    refresh_verification_candidates(session)
    assert getattr(session, "verification_candidates_refresh_failed", False) is False
    session.record_verification(
        "python -m pytest", 1, True, exit_code=0,
        workspace_revision=1, workspace_fingerprint=FP, cwd=".",
    )
    assert evaluate(session, "done", context=None).complete is True


def test_legal_empty_is_distinct_from_failure():
    session = make_session()
    session.verification_candidate_loader = lambda: ()
    refresh_verification_candidates(session)
    assert getattr(session, "verification_candidates_refresh_failed", False) is False
    # Legal empty clears the selected requirement (no configured check).
    assert session.selected_verification is None
