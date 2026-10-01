"""Latest unknown verification result blocks both completion paths.

Locks P1: when the newest observation for one (command, cwd) carries an
unknown result (``passed=False`` with ``exit_code=None``), completion must
refuse on the direct path (no context) and on the engine path (with
execution_evidence), even though an earlier success for the same check
exists. Skipping the unknown row and reviving the old success is fail-open.
Also locks that a newer valid success unblocks (no permanent block) and
that the block survives a save/restore round-trip.
"""
from __future__ import annotations


def _session(tmp_path, *, revision=7):
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
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


def _context(tmp_path, evidence, *, run_id="unknown-latest"):
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


def _record_unknown(session, rev, fp):
    # Unknown result: explicit False with no exit code, via the public API.
    session.record_verification(
        "python -m pytest", 1, False,
        exit_code=None,
        workspace_revision=rev,
        workspace_fingerprint=fp,
        cwd=".",
    )
    # The public API must not invent an exit code for an unknown result.
    assert "exit_code" not in session.verifications[-1]


def test_success_then_unknown_blocks_direct_path(tmp_path):
    from codey.operations.completion_gate import evaluate

    session = _session(tmp_path)
    fp = session.workspace_fingerprint
    rev = session.workspace_revision
    _record_success(session, rev, fp)
    _record_unknown(session, rev, fp)
    assert evaluate(session, "done", context=None).complete is False


def test_success_then_unknown_blocks_engine_path(tmp_path):
    from codey.operations.completion_gate import evaluate
    from codey.runtime.observe.execution_evidence import ExecutionEvidence

    session = _session(tmp_path)
    fp = session.workspace_fingerprint
    rev = session.workspace_revision
    _record_success(session, rev, fp)
    _record_unknown(session, rev, fp)
    evidence = ExecutionEvidence(workspace_revision=rev, workspace_fingerprint=fp)
    assert evaluate(session, "done", context=_context(tmp_path, evidence)).complete is False


def test_success_then_unknown_still_blocks_with_explicit_copy(tmp_path):
    from codey.operations.completion_gate import evaluate
    from codey.runtime.observe.execution_evidence import ExecutionEvidence

    session = _session(tmp_path)
    fp = session.workspace_fingerprint
    rev = session.workspace_revision
    _record_success(session, rev, fp)
    _record_unknown(session, rev, fp)
    restored = _session(tmp_path, revision=rev)
    restored.edited_files = dict(session.edited_files)
    restored.verifications = [dict(r) for r in session.verifications]
    evidence = ExecutionEvidence(workspace_revision=rev, workspace_fingerprint=fp)
    assert evaluate(restored, "done", context=None).complete is False
    assert evaluate(restored, "done", context=_context(tmp_path, evidence)).complete is False


def test_unknown_then_new_success_unblocks_both_paths(tmp_path):
    from codey.operations.completion_gate import evaluate
    from codey.runtime.observe.execution_evidence import ExecutionEvidence

    session = _session(tmp_path)
    fp = session.workspace_fingerprint
    rev = session.workspace_revision
    _record_success(session, rev, fp)
    _record_unknown(session, rev, fp)
    _record_success(session, rev, fp)
    evidence = ExecutionEvidence(workspace_revision=rev, workspace_fingerprint=fp)
    assert evaluate(session, "done", context=None).complete is True
    assert evaluate(session, "done", context=_context(tmp_path, evidence)).complete is True
