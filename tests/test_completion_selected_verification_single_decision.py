"""Unified completion decision: selected verification cannot be replaced.

Same fact must give the same verdict with and without evidence context.
Specifying pytest but only observing compileall success must block in both
paths; observing the specified pytest success must pass in both.
"""
from __future__ import annotations

import pytest

from codey.completion.verification_policy import VerificationCandidate
from codey.operations.completion_gate import evaluate
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.runtime.observe.execution_evidence import ExecutionEvidence
from codey.workspace.revision import workspace_fingerprint


def make_session(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    session = TaskSession(
        policy=TaskPolicy(grants=frozenset({"control", "project.read", "project.write", "project.verify"})),
        task_kind="project",
        project=str(tmp_path),
        max_turns=3,
    )
    session.record_edit("a.py", revision=1)
    session.set_workspace_state(7, workspace_fingerprint(str(tmp_path)))
    session.selected_verification = VerificationCandidate(
        command="python -m pytest", cwd=".", source="project",
    )
    return session


def _context(session, tmp_path, run_id="selected-check"):
    return {
        "execution_evidence": ExecutionEvidence(
            workspace_revision=session.workspace_revision,
            workspace_fingerprint=session.workspace_fingerprint,
        ),
        "project": str(tmp_path),
        "scope_files": ("a.py",),
        "run_id": run_id,
        "task": "fix a.py",
    }


@pytest.mark.parametrize("with_evidence", [False, True])
@pytest.mark.parametrize(
    "observed, expected",
    [("python -m compileall", False), ("python -m pytest", True)],
)
def test_selected_verification_is_not_replaced(tmp_path, with_evidence, observed, expected):
    session = make_session(tmp_path)
    session.record_verification(
        observed, 1, True, exit_code=0,
        workspace_revision=session.workspace_revision,
        workspace_fingerprint=session.workspace_fingerprint,
        cwd=".",
    )
    context = _context(session, tmp_path) if with_evidence else None
    verdict = evaluate(session, "done", context=context)
    assert verdict.complete is expected


@pytest.mark.parametrize("with_evidence", [False, True])
def test_subdirectory_cwd_is_preserved(tmp_path, with_evidence):
    (tmp_path / "pkg").mkdir(exist_ok=True)
    (tmp_path / "pkg" / "a.py").write_text("x = 1\n", encoding="utf-8")
    session = TaskSession(
        policy=TaskPolicy(grants=frozenset({"control", "project.read", "project.write", "project.verify"})),
        task_kind="project",
        project=str(tmp_path),
        max_turns=3,
    )
    session.record_edit("pkg/a.py", revision=1)
    session.set_workspace_state(9, workspace_fingerprint(str(tmp_path)))
    session.selected_verification = VerificationCandidate(
        command="python -m pytest tests/test_a.py", cwd="pkg", source="project",
    )
    session.record_verification(
        "python -m pytest tests/test_a.py", 1, True, exit_code=0,
        workspace_revision=session.workspace_revision,
        workspace_fingerprint=session.workspace_fingerprint,
        cwd="pkg",
    )
    context = None
    if with_evidence:
        context = {
            "execution_evidence": ExecutionEvidence(
                workspace_revision=session.workspace_revision,
                workspace_fingerprint=session.workspace_fingerprint,
            ),
            "project": str(tmp_path),
            "scope_files": ("pkg/a.py",),
            "run_id": "cwd-check",
            "task": "fix pkg/a.py",
        }
    verdict = evaluate(session, "done", context=context)
    assert verdict.complete is True


@pytest.mark.parametrize("with_evidence", [False, True])
def test_cwd_mismatch_without_coverage_blocks(tmp_path, with_evidence):
    (tmp_path / "pkg").mkdir(exist_ok=True)
    (tmp_path / "pkg" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "other").mkdir(exist_ok=True)
    session = TaskSession(
        policy=TaskPolicy(grants=frozenset({"control", "project.read", "project.write", "project.verify"})),
        task_kind="project",
        project=str(tmp_path),
        max_turns=3,
    )
    session.record_edit("pkg/a.py", revision=1)
    session.set_workspace_state(11, workspace_fingerprint(str(tmp_path)))
    session.selected_verification = VerificationCandidate(
        command="python -m pytest", cwd="pkg", source="project",
    )
    # Same command but sibling cwd: must not cover pkg/a.py.
    session.record_verification(
        "python -m pytest", 1, True, exit_code=0,
        workspace_revision=session.workspace_revision,
        workspace_fingerprint=session.workspace_fingerprint,
        cwd="other",
    )
    context = None
    if with_evidence:
        context = {
            "execution_evidence": ExecutionEvidence(
                workspace_revision=session.workspace_revision,
                workspace_fingerprint=session.workspace_fingerprint,
            ),
            "project": str(tmp_path),
            "scope_files": ("pkg/a.py",),
            "run_id": "cwd-mismatch",
            "task": "fix pkg/a.py",
        }
    verdict = evaluate(session, "done", context=context)
    # Either the selected check is uncovered or the cwd does not cover the
    # scope; both paths must agree to block.
    assert verdict.complete is False


@pytest.mark.parametrize("with_evidence", [False, True])
def test_forbidden_verification_does_not_swallow_required_edit(tmp_path, with_evidence):
    session = TaskSession(
        policy=TaskPolicy(grants=frozenset({"control", "project.read", "project.write", "project.verify"})),
        task_kind="project",
        project=str(tmp_path),
        max_turns=3,
    )
    session.project_changes_required = True
    session.verification_forbidden = True
    context = None
    if with_evidence:
        context = {
            "execution_evidence": ExecutionEvidence(
                workspace_revision=session.workspace_revision,
                workspace_fingerprint=session.workspace_fingerprint,
            ),
            "project": str(tmp_path),
            "scope_files": (),
            "run_id": "forbidden-no-edit",
            "task": "fix a.py",
        }
    verdict = evaluate(session, "done", context=context)
    assert verdict.complete is False


@pytest.mark.parametrize("with_evidence", [False, True])
def test_forbidden_with_real_edit_defers_to_other_checks(tmp_path, with_evidence):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    session = TaskSession(
        policy=TaskPolicy(grants=frozenset({"control", "project.read", "project.write", "project.verify"})),
        task_kind="project",
        project=str(tmp_path),
        max_turns=3,
    )
    session.record_edit("a.py", revision=1)
    session.set_workspace_state(13, workspace_fingerprint(str(tmp_path)))
    session.project_changes_required = True
    session.verification_forbidden = True
    context = None
    if with_evidence:
        context = {
            "execution_evidence": ExecutionEvidence(
                workspace_revision=session.workspace_revision,
                workspace_fingerprint=session.workspace_fingerprint,
            ),
            "project": str(tmp_path),
            "scope_files": ("a.py",),
            "run_id": "forbidden-with-edit",
            "task": "fix a.py",
        }
    verdict = evaluate(session, "done", context=context)
    # Verification is exempt; the edit requirement is satisfied so the gate
    # must not fail on missing verification (other domains may still block,
    # but the project check itself must not demand a run).
    from codey.operations.project_completion_checks import project_completion_checks

    rows = project_completion_checks(session, context)
    by_id = {r.check_id: r.status for r in rows}
    assert by_id.get("relevant_verification") == "not_applicable"
