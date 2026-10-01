"""Workspace revision accepts only exact ints, never bool/str/float.

Locks: ``valid_workspace_revision`` returns 0 for ``True``, ``"1"``,
``1.0`` (previously coerced to 1 via ``int()``). Verification ``revision``
restore likewise rejects non-exact ints. Saving never launders these types.
"""
from __future__ import annotations

import pytest


@pytest.mark.parametrize("bad", [True, False, "1", " 1 ", 1.0, 0.0, None, float("inf")])
def test_valid_workspace_revision_rejects_non_exact_int(bad):
    from codey.workspace.revision import valid_workspace_revision

    assert valid_workspace_revision(bad) == 0


def test_valid_workspace_revision_accepts_exact_int():
    from codey.workspace.revision import INITIAL_WORKSPACE_REVISION, valid_workspace_revision

    assert valid_workspace_revision(1) == 1
    assert valid_workspace_revision(INITIAL_WORKSPACE_REVISION) == INITIAL_WORKSPACE_REVISION
    assert valid_workspace_revision(0) == 0


@pytest.mark.parametrize("bad_rev", [True, "1", 1.0])
def test_verification_illegal_revision_never_matches_latest(tmp_path, bad_rev):
    from codey.operations.completion_gate import evaluate
    from codey.operations.project_completion_checks import _session_latest_verification
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.workspace.revision import workspace_fingerprint

    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    fp = workspace_fingerprint(str(tmp_path))
    session = TaskSession(
        policy=TaskPolicy(grants=frozenset({
            "control", "project.read", "project.write", "project.verify",
        })),
        task_kind="project",
        project=str(tmp_path),
    )
    session.record_edit("a.py", revision=1)
    session.set_workspace_state(7, fp)
    session.verifications = [
        {
            "command": "python -m pytest",
            "revision": bad_rev,
            "passed": True,
            "exit_code": 0,
            "workspace_revision": 7,
            "workspace_fingerprint": fp,
            "cwd": ".",
        }
    ]
    # Gate view: illegal revision never matches the latest edit (blocked).
    assert _session_latest_verification(session) is None
    assert evaluate(session, "done", context=None).complete is False
