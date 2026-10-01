"""Verification exemption has one owner: the session task requirement.

Locks P2: ``session.verification_forbidden`` decides exemption on every
completion path. An evidence-only context must not become a second source:
omitting or conflicting ``verification_forbidden`` in the context never
changes the verdict. The direct path and the engine path agree.
"""
from __future__ import annotations


def _session(tmp_path, *, forbidden: bool):
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy

    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    session = TaskSession(
        policy=TaskPolicy(grants=frozenset({
            "control", "project.read", "project.write", "project.verify",
        })),
        task_kind="project",
        project=str(tmp_path),
    )
    session.record_edit("a.py", revision=1)
    session.verification_forbidden = forbidden
    return session


def _context(tmp_path, evidence, **extra):
    base = {
        "execution_evidence": evidence,
        "project": str(tmp_path),
        "scope_files": ("a.py",),
        "run_id": "forbidden-single-source",
        "task": "fix",
    }
    base.update(extra)
    return base


def test_forbidden_session_allows_with_and_without_context(tmp_path):
    from codey.operations.completion_gate import evaluate
    from codey.runtime.observe.execution_evidence import ExecutionEvidence

    session = _session(tmp_path, forbidden=True)
    evidence = ExecutionEvidence()
    # No context: direct path exempts.
    assert evaluate(session, "done", context=None).complete is True
    # Context without the field: engine path must agree (session owns it).
    assert evaluate(session, "done", context=_context(tmp_path, evidence)).complete is True
    # Context repeats the same value: still allows.
    ctx = _context(tmp_path, evidence, verification_forbidden=True)
    assert evaluate(session, "done", context=ctx).complete is True


def test_conflicting_context_does_not_override_session_exemption(tmp_path):
    from codey.operations.completion_gate import evaluate
    from codey.runtime.observe.execution_evidence import ExecutionEvidence

    session = _session(tmp_path, forbidden=True)
    evidence = ExecutionEvidence()
    # A stale/missing context value must not revoke the task's exemption.
    ctx = _context(tmp_path, evidence, verification_forbidden=False)
    assert evaluate(session, "done", context=ctx).complete is True


def test_required_session_not_exempted_by_context_flag(tmp_path):
    from codey.operations.completion_gate import evaluate
    from codey.runtime.observe.execution_evidence import ExecutionEvidence

    session = _session(tmp_path, forbidden=False)
    evidence = ExecutionEvidence()
    assert evaluate(session, "done", context=None).complete is False
    # Context claiming exemption must not grant it when the task requires verification.
    ctx = _context(tmp_path, evidence, verification_forbidden=True)
    assert evaluate(session, "done", context=ctx).complete is False
