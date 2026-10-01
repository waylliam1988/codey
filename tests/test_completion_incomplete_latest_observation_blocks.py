"""Incomplete latest verification blocks completion, old success never revives.

Locks P1: when the latest observation for one (command, cwd) lacks workspace
identity, the completion entry must refuse even though an earlier success for
the same check exists. Skipping the incomplete row and reusing the old success
is a fail-open projection bug.
"""
from __future__ import annotations

import pytest

from codey.operations.completion_gate import evaluate
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.runtime.observe.execution_evidence import ExecutionEvidence


def _session(tmp_path, *, revision=7):
    from codey.workspace.revision import workspace_fingerprint

    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    real_fp = workspace_fingerprint(str(tmp_path))
    assert real_fp
    session = TaskSession(
        policy=TaskPolicy(grants=frozenset({
            "control", "project.read", "project.write", "project.verify",
        })),
        task_kind="project",
        project=str(tmp_path),
    )
    session.record_edit("a.py", revision=1)
    session.set_workspace_state(revision, real_fp)
    return session


def _context(tmp_path, evidence, *, run_id="incomplete-latest"):
    return {
        "execution_evidence": evidence,
        "project": str(tmp_path),
        "scope_files": ("a.py",),
        "run_id": run_id,
        "task": "fix",
    }


def _record_success(session, rev, fp):
    session.record_verification(
        "python -m pytest", 1, True,
        exit_code=0,
        workspace_revision=rev,
        workspace_fingerprint=fp,
        cwd=".",
    )


def _record_incomplete_failure(session, kind, rev, fp):
    if kind == "missing_revision":
        session.record_verification(
            "python -m pytest", 1, False, exit_code=1, cwd=".",
        )
        # record_verification stores no workspace_revision when omitted;
        # ensure the row really lacks identity.
        assert "workspace_revision" not in session.verifications[-1]
    elif kind == "missing_fingerprint":
        session.record_verification(
            "python -m pytest", 1, False, exit_code=1,
            workspace_revision=rev, cwd=".",
        )
        assert "workspace_fingerprint" not in session.verifications[-1]
    elif kind == "bad_fingerprint":
        session.record_verification(
            "python -m pytest", 1, False, exit_code=1,
            workspace_revision=rev,
            workspace_fingerprint="bad-fingerprint",
            cwd=".",
        )
    else:
        raise AssertionError(kind)


@pytest.mark.parametrize("kind", ["missing_revision", "missing_fingerprint", "bad_fingerprint"])
@pytest.mark.parametrize("with_context", [False, True])
def test_incomplete_latest_failure_blocks_despite_earlier_success(tmp_path, kind, with_context):
    session = _session(tmp_path)
    fp = session.workspace_fingerprint
    rev = session.workspace_revision
    _record_success(session, rev, fp)
    _record_incomplete_failure(session, kind, rev, fp)
    context = None
    if with_context:
        evidence = ExecutionEvidence(workspace_revision=rev, workspace_fingerprint=fp)
        context = _context(tmp_path, evidence)
    assert evaluate(session, "done", context=context).complete is False


def test_incomplete_old_record_followed_by_valid_success_completes(tmp_path):
    session = _session(tmp_path)
    fp = session.workspace_fingerprint
    rev = session.workspace_revision
    _record_incomplete_failure(session, "missing_revision", rev, fp)
    _record_success(session, rev, fp)
    evidence = ExecutionEvidence(workspace_revision=rev, workspace_fingerprint=fp)
    assert evaluate(session, "done", context=None).complete is True
    assert evaluate(session, "done", context=_context(tmp_path, evidence)).complete is True


def test_incomplete_latest_still_blocks_after_session_restore(tmp_path):
    session = _session(tmp_path)
    fp = session.workspace_fingerprint
    rev = session.workspace_revision
    _record_success(session, rev, fp)
    _record_incomplete_failure(session, "missing_fingerprint", rev, fp)
    restored = TaskSession.from_payload(session.to_payload(), policy=session.policy)
    evidence = ExecutionEvidence(workspace_revision=rev, workspace_fingerprint=fp)
    assert evaluate(restored, "done", context=None).complete is False
    assert evaluate(restored, "done", context=_context(tmp_path, evidence)).complete is False
