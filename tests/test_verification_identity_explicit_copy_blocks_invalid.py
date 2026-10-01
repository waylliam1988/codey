"""Verification identity never gains credibility through state reuse.

Locks P1: a verification ``workspace_revision`` of illegal type (``True``,
``"1"``, ``1.0``) must never become a valid integer through explicit state
reuse. Illegal rows block the completion gate directly; legal, missing, and
failed rows keep the same verdict after an explicit copy; repeated copies
keep the same verdict.
"""
from __future__ import annotations

import copy

import pytest


def _base_session(tmp_path):
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.workspace.revision import workspace_fingerprint

    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    fp = workspace_fingerprint(str(tmp_path))
    assert fp
    session = TaskSession(
        policy=TaskPolicy(grants=frozenset({
            "control", "project.read", "project.write", "project.verify",
        })),
        task_kind="project",
        project=str(tmp_path),
    )
    session.record_edit("a.py", revision=1)
    session.set_workspace_state(7, fp)
    session.record_verification(
        "python -m pytest", 1, True, exit_code=0,
        workspace_revision=7, workspace_fingerprint=fp, cwd=".",
    )
    return session, fp


def _explicit_copy(session):
    """Explicit state copy: same factory plus copied facts, no restore path."""
    from codey.operations.task_session import TaskSession

    copied = TaskSession(
        policy=session.policy,
        task_kind=session.task_kind,
        project=session.project,
        max_turns=session.max_turns,
    )
    copied.edited_files = copy.deepcopy(session.edited_files)
    copied.verifications = copy.deepcopy(session.verifications)
    copied.read_files = set(session.read_files)
    copied.workspace_revision = session.workspace_revision
    copied.workspace_fingerprint = session.workspace_fingerprint
    return copied


@pytest.mark.parametrize("bad_rev", [True, "1", 1.0])
def test_illegal_revision_blocks_gate_and_copy_still_blocks(tmp_path, bad_rev):
    from codey.operations.completion_gate import evaluate

    session, _ = _base_session(tmp_path)
    # Directly construct the illegal row in the live session.
    session.verifications[0]["workspace_revision"] = bad_rev
    assert evaluate(session, "done", context=None).complete is False
    # Explicit copy keeps the same illegal value and still blocks.
    copied = _explicit_copy(session)
    assert copied.verifications[0]["workspace_revision"] is bad_rev or copied.verifications[0]["workspace_revision"] == bad_rev
    assert type(copied.verifications[0]["workspace_revision"]) is type(bad_rev)
    assert evaluate(copied, "done", context=None).complete is False


@pytest.mark.parametrize("bad_rev", [True, "1", 1.0])
def test_explicit_copy_never_washes_illegal_revision(tmp_path, bad_rev):
    """Explicit reuse must not turn an illegal type into valid 1."""
    from codey.operations.completion_gate import evaluate

    session, _ = _base_session(tmp_path)
    session.verifications[0]["workspace_revision"] = bad_rev
    assert evaluate(session, "done", context=None).complete is False
    # The point: a second explicit copy must not flip the verdict to pass.
    # The raw value never equals a clean integer after the copy rules are
    # applied to a session carrying the illegal value.
    copied = _explicit_copy(session)
    reparsed = copied.verifications[0]["workspace_revision"]
    assert not (type(reparsed) is int and reparsed == 1), (
        f"explicit copy washed {bad_rev!r} into valid int 1"
    )
    assert evaluate(copied, "done", context=None).complete is False
    # Repeated copies keep blocking.
    third = _explicit_copy(copied)
    assert evaluate(third, "done", context=None).complete is False


def test_legal_identity_explicit_copy_stays_passing(tmp_path):
    from codey.operations.completion_gate import evaluate

    session, _ = _base_session(tmp_path)
    first = _explicit_copy(session)
    second = _explicit_copy(first)
    assert first.verifications[0]["workspace_revision"] == 7
    assert second.verifications[0]["workspace_revision"] == 7
    assert type(second.verifications[0]["workspace_revision"]) is int
    assert evaluate(second, "done", context=None).complete is True
    # Repeated copies keep the same verdict.
    third = _explicit_copy(second)
    assert evaluate(third, "done", context=None).complete is True


def test_missing_identity_explicit_copy_still_blocks(tmp_path):
    from codey.operations.completion_gate import evaluate
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.workspace.revision import workspace_fingerprint

    (tmp_path / "b.py").write_text("y = 2\n", encoding="utf-8")
    fp = workspace_fingerprint(str(tmp_path))
    session = TaskSession(
        policy=TaskPolicy(grants=frozenset({
            "control", "project.read", "project.write", "project.verify",
        })),
        task_kind="project",
        project=str(tmp_path),
    )
    session.record_edit("b.py", revision=1)
    session.set_workspace_state(7, fp)
    session.record_verification("python -m pytest", 1, False, exit_code=1, cwd=".")
    assert "workspace_revision" not in session.verifications[-1]
    restored = _explicit_copy(session)
    assert "workspace_revision" not in restored.verifications[-1]
    assert evaluate(restored, "done", context=None).complete is False


def test_legal_failure_explicit_copy_stays_blocking(tmp_path):
    from codey.operations.completion_gate import evaluate

    session, _ = _base_session(tmp_path)
    session.verifications[0]["passed"] = False
    session.verifications[0]["exit_code"] = 1
    first = _explicit_copy(session)
    second = _explicit_copy(first)
    assert second.verifications[0]["passed"] is False
    assert second.verifications[0]["exit_code"] == 1
    assert evaluate(second, "done", context=None).complete is False
