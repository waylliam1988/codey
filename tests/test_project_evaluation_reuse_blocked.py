"""Stale project_evaluation must not bypass current requirements.

Same current facts must block with or without a precomputed proof:
- refresh_failed blocks
- missing required edits blocks
- workspace changed after verification blocks
- fresh legal verification passes
"""
from __future__ import annotations

from codey.completion.verification_policy import VerificationCandidate
from codey.operations.completion_gate import evaluate
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.runtime.observe.execution_evidence import ExecutionEvidence

FP = "sha256:" + "b" * 64
FP2 = "sha256:" + "c" * 64


def _session():
    s = TaskSession(
        policy=TaskPolicy(grants=frozenset({"control", "project.read", "project.write", "project.verify"})),
        task_kind="project",
        project="",
        max_turns=3,
    )
    s.edited_files = {"a.py": 1}
    s.set_workspace_state(1, FP)
    req = VerificationCandidate("python -m pytest", ".", "project")
    s.verification_candidates = (req,)
    s.verification_candidates_epoch = 1
    s.selected_verification = req
    return s


def _stale_passing_evaluation():
    """A precomputed passing proof from an older workspace (must not be reused)."""
    from types import SimpleNamespace

    from codey.completion.contract import CHECK_PASS, completion_check

    row = completion_check("relevant_verification", CHECK_PASS, "")
    assert row is not None
    proof = SimpleNamespace(checks=[row], evidence_refs=(), status="complete", satisfied=True)
    decision = SimpleNamespace(proof=proof)
    return SimpleNamespace(decision=decision)


def _ctx(session, evaluation=None, task_changed=True):
    ctx = {
        "execution_evidence": ExecutionEvidence(
            workspace_revision=session.workspace_revision,
            workspace_fingerprint=session.workspace_fingerprint,
        ),
        "project": "",
        "scope_files": ("a.py",),
        "task_changed": task_changed,
        "run_id": "r-fresh",
        "task": "fix a.py",
        "selected_check": session.selected_verification,
    }
    if evaluation is not None:
        ctx["project_evaluation"] = evaluation
    return ctx


def test_refresh_failed_blocks_with_and_without_stale_proof():
    s = _session()
    s.record_verification("python -m pytest", 1, True, exit_code=0,
                          workspace_revision=1, workspace_fingerprint=FP, cwd=".")
    stale = _stale_passing_evaluation()
    # Now mark refresh failed (loader broke after proof was minted).
    s.verification_candidates_refresh_failed = True
    assert evaluate(s, "done", context=_ctx(s, None)).complete is False
    assert evaluate(s, "done", context=_ctx(s, stale)).complete is False


def test_missing_edits_blocks_with_and_without_stale_proof():
    s = _session()
    s.project_changes_required = True
    s.record_verification("python -m pytest", 1, True, exit_code=0,
                          workspace_revision=1, workspace_fingerprint=FP, cwd=".")
    stale = _stale_passing_evaluation()
    # Simulate current facts: no edits (task_changed False, empty scope).
    s.edited_files = {}
    ctx_none = dict(_ctx(s, None, task_changed=False))
    ctx_none["scope_files"] = ()
    ctx_stale = dict(_ctx(s, stale, task_changed=False))
    ctx_stale["scope_files"] = ()
    assert evaluate(s, "done", context=ctx_none).complete is False
    assert evaluate(s, "done", context=ctx_stale).complete is False


def test_workspace_changed_blocks_stale_proof():
    s = _session()
    s.record_verification("python -m pytest", 1, True, exit_code=0,
                          workspace_revision=1, workspace_fingerprint=FP, cwd=".")
    stale = _stale_passing_evaluation()
    # Workspace moved after verification: current FP2, old proof still references FP.
    s.set_workspace_state(1, FP2)
    assert evaluate(s, "done", context=_ctx(s, None)).complete is False
    assert evaluate(s, "done", context=_ctx(s, stale)).complete is False


def test_fresh_legal_verification_passes_without_stale_proof():
    s = _session()
    s.record_verification("python -m pytest", 1, True, exit_code=0,
                          workspace_revision=1, workspace_fingerprint=FP, cwd=".")
    assert evaluate(s, "done", context=_ctx(s, None)).complete is True


def test_operation_confirmed_changes_complete_despite_incomplete_memory():
    """Operation confirmed real edits; memory edited_files incomplete but scope proves it."""
    s = _session()
    s.edited_files = {}
    s.record_verification("python -m pytest", 1, True, exit_code=0,
                          workspace_revision=1, workspace_fingerprint=FP, cwd=".")
    ctx = _ctx(s, None, task_changed=True)
    ctx["scope_files"] = ("a.py",)
    # Session memory empty, but operation scope + evidence confirm the change.
    assert evaluate(s, "done", context=ctx).complete is True
