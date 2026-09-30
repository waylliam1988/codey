"""Verification identity must bind exactly to the current workspace version.

Locks: missing/malformed revision or fingerprint never completes; stale
revision never matches even when the fingerprint is equal; evidence
projection copies the original command/cwd/revision/fingerprint without
re-stamping them to the current evidence version.
"""
from __future__ import annotations

from codey.operations.completion_gate import evaluate
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.workspace.revision import workspace_fingerprint


def _project_session(tmp_path, *, revision=7):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    fp = workspace_fingerprint(tmp_path)
    session = TaskSession(
        policy=TaskPolicy(
            grants=frozenset({"control", "project.read", "project.write", "project.verify"})
        ),
        task_kind="project",
        project=str(tmp_path),
        max_turns=3,
    )
    session.record_edit("a.py", revision=1)
    session.set_workspace_state(revision, fp)
    return session, fp


def test_verification_without_workspace_revision_cannot_complete(tmp_path):
    session, fp = _project_session(tmp_path)
    session.record_verification(
        "pytest", revision=1, passed=True, exit_code=0,
        workspace_fingerprint=fp,
    )
    assert evaluate(session, "done").complete is False


def test_verification_without_fingerprint_cannot_complete(tmp_path):
    session, _ = _project_session(tmp_path)
    session.record_verification(
        "pytest", revision=1, passed=True, exit_code=0,
        workspace_revision=session.workspace_revision,
    )
    assert evaluate(session, "done").complete is False


def test_stale_revision_with_matching_fingerprint_cannot_complete(tmp_path):
    session, fp = _project_session(tmp_path, revision=7)
    session.record_verification(
        "pytest", revision=1, passed=True, exit_code=0,
        workspace_revision=2, workspace_fingerprint=fp, cwd=".",
    )
    from codey.operations import project_completion_checks as pcc

    assert pcc._verification_identity_matches(
        session.verifications[-1], session.workspace_fingerprint, session.workspace_revision,
    ) is False
    assert evaluate(session, "done").complete is False


def test_bool_string_zero_revision_cannot_complete(tmp_path):
    from codey.operations import project_completion_checks as pcc

    session, fp = _project_session(tmp_path, revision=7)
    for bad_rev in (True, "7", 0, -1, None):
        row = {
            "command": "pytest", "cwd": ".", "revision": 1, "passed": True,
            "exit_code": 0, "workspace_revision": bad_rev, "workspace_fingerprint": fp,
        }
        assert pcc._verification_identity_matches(row, fp, 7) is False, bad_rev


def test_session_identity_requires_matching_revision_even_when_fingerprint_matches(tmp_path):
    from codey.operations import project_completion_checks as pcc

    session, fp = _project_session(tmp_path, revision=7)
    latest = {"workspace_fingerprint": fp, "workspace_revision": 2}
    assert pcc._session_identity_matches(session, latest, 7, fp) is False


def test_stale_session_verification_does_not_project_into_evidence(tmp_path):
    from codey.operations import project_completion_checks as pcc
    from codey.runtime.observe.execution_evidence import ExecutionEvidence

    session, fp = _project_session(tmp_path, revision=7)
    session.record_verification(
        "pytest", revision=1, passed=True, exit_code=0,
        workspace_revision=2, workspace_fingerprint=fp, cwd="pkg",
    )
    evidence = ExecutionEvidence(workspace_revision=7, workspace_fingerprint=fp)
    projected = pcc._evidence_with_session_facts(evidence, session)
    assert list(projected.checks_after_edit) == []


def test_session_append_preserves_cwd_and_full_identity(tmp_path):
    from codey.operations import project_completion_checks as pcc
    from codey.runtime.observe.execution_evidence import ExecutionEvidence

    _, fp = _project_session(tmp_path, revision=7)
    evidence = ExecutionEvidence(workspace_revision=7, workspace_fingerprint=fp)
    row = {
        "command": "pytest", "cwd": "pkg", "revision": 1, "passed": True,
        "exit_code": 0, "workspace_revision": 7, "workspace_fingerprint": fp,
    }
    pcc._append_session_check(evidence, row)
    rows = list(evidence.checks_after_edit)
    assert rows and rows[-1].cwd == "pkg"
    assert rows[-1].workspace_revision == 7
    assert rows[-1].workspace_fingerprint == fp


def test_matching_revision_and_fingerprint_can_proceed(tmp_path):
    session, fp = _project_session(tmp_path, revision=7)
    session.record_verification(
        "pytest", revision=1, passed=True, exit_code=0,
        workspace_revision=7, workspace_fingerprint=fp, cwd=".",
    )
    from codey.operations import project_completion_checks as pcc

    assert pcc._verification_identity_matches(
        session.verifications[-1], fp, 7,
    ) is True
    assert evaluate(session, "done").complete is True


def test_evidence_projection_preserves_original_cwd_and_version(tmp_path):
    from codey.operations import project_completion_checks as pcc
    from codey.runtime.observe.execution_evidence import ExecutionEvidence

    session, fp = _project_session(tmp_path, revision=7)
    session.record_verification(
        "pytest", revision=1, passed=True, exit_code=0,
        workspace_revision=7, workspace_fingerprint=fp, cwd="pkg",
    )
    evidence = ExecutionEvidence(workspace_revision=7, workspace_fingerprint=fp)
    projected = pcc._evidence_with_session_facts(evidence, session)
    rows = list(projected.checks_after_edit)
    assert rows, "matching verification must project one check"
    assert rows[-1].cwd == "pkg"
    assert rows[-1].workspace_revision == 7
    assert rows[-1].workspace_fingerprint == fp
    assert rows[-1].command == "pytest"


def test_same_command_different_cwd_do_not_overwrite_each_other(tmp_path):
    from codey.runtime.observe.execution_evidence import CheckEvidence, ExecutionEvidence

    _, fp = _project_session(tmp_path, revision=7)
    evidence = ExecutionEvidence(workspace_revision=7, workspace_fingerprint=fp)
    evidence._append_check(
        evidence.checks_after_edit,
        CheckEvidence("pytest", "pkg", exit_code=0, workspace_revision=7, workspace_fingerprint=fp),
    )
    evidence._append_check(
        evidence.checks_after_edit,
        CheckEvidence("pytest", ".", exit_code=0, workspace_revision=7, workspace_fingerprint=fp),
    )
    by_cwd = {(row.command, row.cwd) for row in evidence.checks_after_edit}
    assert ("pytest", "pkg") in by_cwd
    assert ("pytest", ".") in by_cwd
