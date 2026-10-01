"""Completion projection keeps the latest observation, with or without context.

Locks that the completion entry gives the same verdict whether the caller
supplies an execution-evidence context or not:

- success then failure blocks in both paths (latest failure wins);
- failure then success completes in both paths (latest success wins);
- stale-revision success never completes in either path;
- same command in different cwd keeps independent results;
- repeated evaluation stays stable without duplicating evidence.
"""
from __future__ import annotations

import pytest

from codey.operations.completion_gate import evaluate
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.runtime.observe.execution_evidence import CheckEvidence, ExecutionEvidence


def _session(tmp_path, *, revision=7, fingerprint=None):
    from codey.workspace.revision import workspace_fingerprint

    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    real_fp = workspace_fingerprint(str(tmp_path))
    assert real_fp, "real workspace fingerprint must exist"
    session = TaskSession(
        policy=TaskPolicy(grants=frozenset({
            "control", "project.read", "project.write", "project.verify",
        })),
        task_kind="project",
        project=str(tmp_path),
    )
    session.record_edit("a.py", revision=1)
    session.set_workspace_state(revision, fingerprint or real_fp)
    return session


def _evidence(revision=7, fingerprint=None, *, checks=()):
    evidence = ExecutionEvidence(
        workspace_revision=revision,
        workspace_fingerprint=fingerprint,
    )
    for row in checks:
        evidence.checks_after_edit.append(row)
    return evidence


def _context(tmp_path, evidence, *, run_id="latest-observation"):
    return {
        "execution_evidence": evidence,
        "project": str(tmp_path),
        "scope_files": ("a.py",),
        "run_id": run_id,
        "task": "fix",
    }


@pytest.mark.parametrize("with_context", [False, True])
def test_success_then_failure_blocks_with_or_without_context(tmp_path, with_context):
    session = _session(tmp_path)
    fp = session.workspace_fingerprint
    rev = session.workspace_revision
    for passed, exit_code in [(True, 0), (False, 1)]:
        session.record_verification(
            "python -m pytest", 1, passed,
            exit_code=exit_code,
            workspace_revision=rev,
            workspace_fingerprint=fp,
            cwd=".",
        )
    context = None
    if with_context:
        old = CheckEvidence(
            "python -m pytest", ".",
            exit_code=0,
            workspace_revision=rev,
            workspace_fingerprint=fp,
        )
        context = _context(tmp_path, _evidence(rev, fp, checks=(old,)))

    assert evaluate(session, "done", context=context).complete is False


@pytest.mark.parametrize("with_context", [False, True])
def test_failure_then_success_completes_with_or_without_context(tmp_path, with_context):
    session = _session(tmp_path)
    fp = session.workspace_fingerprint
    rev = session.workspace_revision
    for passed, exit_code in [(False, 1), (True, 0)]:
        session.record_verification(
            "python -m pytest", 1, passed,
            exit_code=exit_code,
            workspace_revision=rev,
            workspace_fingerprint=fp,
            cwd=".",
        )
    context = None
    if with_context:
        old = CheckEvidence(
            "python -m pytest", ".",
            exit_code=1,
            workspace_revision=rev,
            workspace_fingerprint=fp,
        )
        evidence = _evidence(rev, fp)
        evidence.failed_checks_after_edit.append(old)
        context = _context(tmp_path, evidence)

    assert evaluate(session, "done", context=context).complete is True


@pytest.mark.parametrize("with_context", [False, True])
def test_stale_revision_success_never_completes(tmp_path, with_context):
    from codey.workspace.revision import workspace_fingerprint

    session = _session(tmp_path)
    fp = session.workspace_fingerprint
    rev = session.workspace_revision
    session.record_verification(
        "python -m pytest", 1, True,
        exit_code=0,
        workspace_revision=rev,
        workspace_fingerprint=fp,
        cwd=".",
    )
    # Workspace moved on without fresh verification: change files so the
    # real fingerprint moves, then keep the new workspace identity.
    (tmp_path / "a.py").write_text("x = 2\n", encoding="utf-8")
    new_fp = workspace_fingerprint(str(tmp_path))
    assert new_fp and new_fp != fp
    session.set_workspace_state(rev + 1, new_fp)
    context = None
    if with_context:
        old = CheckEvidence(
            "python -m pytest", ".",
            exit_code=0,
            workspace_revision=rev,
            workspace_fingerprint=fp,
        )
        context = _context(tmp_path, _evidence(rev + 1, new_fp, checks=(old,)))

    assert evaluate(session, "done", context=context).complete is False


@pytest.mark.parametrize("with_context", [False, True])
def test_same_command_different_cwd_keeps_independent_results(tmp_path, with_context):
    session = _session(tmp_path)
    fp = session.workspace_fingerprint
    rev = session.workspace_revision
    session.record_verification(
        "python -m pytest", 1, True,
        exit_code=0,
        workspace_revision=rev,
        workspace_fingerprint=fp,
        cwd="pkg-a",
    )
    session.record_verification(
        "python -m pytest", 1, False,
        exit_code=1,
        workspace_revision=rev,
        workspace_fingerprint=fp,
        cwd="pkg-b",
    )
    context = None
    if with_context:
        context = _context(tmp_path, _evidence(rev, fp))

    # One failing cwd blocks completion even though the sibling passed.
    assert evaluate(session, "done", context=context).complete is False


def test_repeated_evaluation_stays_stable_without_duplication(tmp_path):
    session = _session(tmp_path)
    fp = session.workspace_fingerprint
    rev = session.workspace_revision
    for passed, exit_code in [(True, 0), (False, 1)]:
        session.record_verification(
            "python -m pytest", 1, passed,
            exit_code=exit_code,
            workspace_revision=rev,
            workspace_fingerprint=fp,
            cwd=".",
        )
    evidence = _evidence(rev, fp, checks=(
        CheckEvidence(
            "python -m pytest", ".",
            exit_code=0,
            workspace_revision=rev,
            workspace_fingerprint=fp,
        ),
    ))
    context = _context(tmp_path, evidence)
    first = evaluate(session, "done", context=context)
    count_after_first = (
        len(evidence.checks_after_edit),
        len(evidence.failed_checks_after_edit),
    )
    second = evaluate(session, "done", context=context)
    assert (first.complete, second.complete) == (False, False)
    assert (
        len(evidence.checks_after_edit),
        len(evidence.failed_checks_after_edit),
    ) == count_after_first
