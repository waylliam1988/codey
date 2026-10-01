"""Verification identity survives save/restore without gaining credibility.

Locks P1: a verification ``workspace_revision`` of illegal type (``True``,
``"1"``, ``1.0``) must never become a valid integer through one or more
save/restore cycles. The restore entry rejects illegal identity types with
``RecoveryFailed``; legal, missing, and failed rows round-trip unchanged;
repeated restores keep the same verdict.
"""
from __future__ import annotations

import copy

import pytest


def _base_payload(tmp_path):
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
    payload = session.to_payload()
    return session, payload, fp


@pytest.mark.parametrize("bad_rev", [True, "1", 1.0])
def test_restore_rejects_illegal_revision_type(tmp_path, bad_rev):
    from codey.operations.kernel_errors import RecoveryFailed
    from codey.operations.task_session import TaskSession

    _, payload, _ = _base_payload(tmp_path)
    mutated = copy.deepcopy(payload)
    mutated["verifications"][0]["workspace_revision"] = bad_rev
    with pytest.raises(RecoveryFailed):
        TaskSession.from_payload(mutated)


@pytest.mark.parametrize("bad_rev", [True, "1", 1.0])
def test_payload_serialization_never_washes_illegal_revision(tmp_path, bad_rev):
    """Even the serialized form must not turn an illegal type into valid 1."""
    from codey.operations.task_session import TaskSession

    _, payload, _ = _base_payload(tmp_path)
    mutated = copy.deepcopy(payload)
    mutated["verifications"][0]["workspace_revision"] = bad_rev
    # to_payload is only reachable via a live session; emulate the wash by
    # forcing the illegal row through a session dict and re-serializing.
    # The point: a second save/restore must not flip the verdict to pass.
    # Direct from_payload must already fail (see above); here we assert the
    # raw payload value never equals a clean integer after serialization
    # rules are applied to a session carrying the illegal value.
    session = TaskSession.from_payload(payload)
    # Inject the illegal value as if it came from an untrusted restore that
    # somehow slipped through, then serialize: the output must not be int 1.
    session.verifications[0]["workspace_revision"] = bad_rev
    reparsed = session.to_payload()["verifications"][0]["workspace_revision"]
    assert not (type(reparsed) is int and reparsed == 1), (
        f"serialization washed {bad_rev!r} into valid int 1"
    )


def test_legal_identity_roundtrip_unchanged(tmp_path):
    from codey.operations.completion_gate import evaluate
    from codey.operations.task_session import TaskSession

    session, payload, fp = _base_payload(tmp_path)
    first = TaskSession.from_payload(copy.deepcopy(payload), policy=session.policy)
    second = TaskSession.from_payload(first.to_payload(), policy=session.policy)
    assert first.verifications[0]["workspace_revision"] == 7
    assert second.verifications[0]["workspace_revision"] == 7
    assert type(second.verifications[0]["workspace_revision"]) is int
    assert evaluate(second, "done", context=None).complete is True
    # Repeated restores keep the same verdict.
    third = TaskSession.from_payload(second.to_payload(), policy=session.policy)
    assert evaluate(third, "done", context=None).complete is True


def test_missing_identity_roundtrip_still_blocks(tmp_path):
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
    restored = TaskSession.from_payload(session.to_payload(), policy=session.policy)
    assert "workspace_revision" not in restored.verifications[-1]
    assert evaluate(restored, "done", context=None).complete is False


def test_legal_failure_roundtrip_stays_failure(tmp_path):
    from codey.operations.completion_gate import evaluate
    from codey.operations.task_session import TaskSession

    session, payload, _ = _base_payload(tmp_path)
    payload["verifications"][0]["passed"] = False
    payload["verifications"][0]["exit_code"] = 1
    first = TaskSession.from_payload(copy.deepcopy(payload), policy=session.policy)
    second = TaskSession.from_payload(first.to_payload(), policy=session.policy)
    assert second.verifications[0]["passed"] is False
    assert second.verifications[0]["exit_code"] == 1
    assert evaluate(second, "done", context=None).complete is False
