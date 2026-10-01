"""Recovery trust and type boundaries exercised before implementation changes."""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest


def test_guarded_persisted_replay_rechecks_workspace_after_batch_check() -> None:
    from codey.operations.kernel_recovery import _check_batch_recovery, _guarded_slot_result
    from codey.operations.kernel_recovery_context import RecoveryContext
    from codey.operations.task_session import TaskSession, turn_effect_id
    from codey.policies.task_policy import TaskPolicy
    from codey.runtime.core.models import ToolCall
    from codey.runtime.effects.effect_records import compute_args_digest
    from codey.workspace.revision import WorkspaceIdentity

    old = WorkspaceIdentity.trusted_pair(2, "sha256:" + "ab" * 32)
    new = WorkspaceIdentity.trusted_pair(3, "sha256:" + "cd" * 32)

    class Store:
        state = old

        def current_state(self, _project: object, *, ignored_paths: object = ()) -> object:
            return self.state

    store = Store()
    call = ToolCall(name="edit", args={"path": "a.py", "content": "new"}, call_id="c1")
    identity = turn_effect_id("run", 1, 0)
    session = TaskSession(policy=TaskPolicy(grants=frozenset({"project.write", "control"})))
    session.executed[identity] = {
        "name": "edit",
        "call_id": "c1",
        "args_digest": compute_args_digest(call.args),
        "ok": True,
        "excerpt": "edited",
        "workspace_revision": old.revision,
        "workspace_fingerprint": old.fingerprint,
    }
    context = RecoveryContext(project_path="project", revision_store=store)
    check = _check_batch_recovery(
        session,
        [call],
        {},
        "run",
        1,
        0,
        project_path="project",
        revision_store=store,
        recovery_ctx=context,
    )
    assert check.kind == "NO_MATCH"
    store.state = new
    guarded = _guarded_slot_result(
        session,
        identity,
        call,
        "edit",
        1,
        None,
        None,
        project_path="project",
        revision_store=store,
        recovery_ctx=context,
    )
    assert guarded is not None
    assert guarded.model_text.startswith("ERROR:")


@pytest.mark.parametrize("passed", ["false", 0, False])
def test_completion_gate_rejects_invalid_or_conflicting_passed_with_zero_exit(passed: object) -> None:
    from codey.operations.project_completion_checks import _evidence_with_session_facts

    fingerprint = "sha256:" + "ab" * 32
    evidence = SimpleNamespace(workspace_revision=2, workspace_fingerprint=fingerprint, checks_after_edit=[])
    session = SimpleNamespace(
        edited_files={},
        workspace_revision=2,
        workspace_fingerprint=fingerprint,
        verifications=[
            {
                "command": "pytest",
                "revision": 2,
                "passed": passed,
                "exit_code": 0,
                "workspace_revision": 2,
                "workspace_fingerprint": fingerprint,
            }
        ],
    )
    _, _gaps = _evidence_with_session_facts(evidence, session)
    assert evidence.checks_after_edit == []


@pytest.mark.parametrize("passed", ["false", 0])
def test_record_verification_rejects_non_boolean_passed(passed: object) -> None:
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy

    session = TaskSession(policy=TaskPolicy(grants=frozenset()))
    with pytest.raises(TypeError):
        session.record_verification("pytest", 1, passed)  # type: ignore[arg-type]
    assert session.verifications == []


def test_frame_recovery_rejects_non_boolean_ok_before_replaying_facts() -> None:
    from codey.operations.kernel_errors import RecoveryFailed
    from codey.operations.kernel_recovery_result import spec_from_frame_row
    from codey.runtime.core.models import ToolCall

    row = SimpleNamespace(
        turn=1,
        tool_index=0,
        call=ToolCall(name="read_file", args={"path": "a.py"}),
        outcome=SimpleNamespace(ok="false", model_text="old", audit={}, presentation={}, canonical={}, truncated=False),
    )
    with pytest.raises(RecoveryFailed, match="ok"):
        spec_from_frame_row(row)


def test_invalid_present_event_proof_raises_instead_of_becoming_absent() -> None:
    from codey.operations.kernel_errors import RecoveryFailed
    from codey.operations.kernel_provenance import _EVENT_PROOF_ATTR, event_proof
    from codey.runtime.observe.events import RunEvent

    absent = RunEvent.info("x")
    assert event_proof(absent) is None
    invalid = RunEvent.info("x")
    object.__setattr__(invalid, _EVENT_PROOF_ATTR, object())
    with pytest.raises(RecoveryFailed):
        event_proof(invalid)


def test_session_restore_rejects_malformed_early_field_without_dropping_receipt() -> None:
    from codey.operations.kernel_errors import RecoveryFailed
    from codey.operations.task_session import TaskSession

    payload = {
        "hit_targets": {"h1": {"url": "x", "offset": "broken"}},
        "executed": {"effect": {"name": "edit", "ok": True}},
    }
    with pytest.raises(RecoveryFailed):
        TaskSession.from_payload(payload)


def test_proof_validator_rejects_subclasses_and_coerced_fields() -> None:
    from codey.operations.kernel_errors import RecoveryFailed
    from codey.operations.kernel_provenance import (
        TrustedWorkspaceProof,
        _trusted_workspace_proof,
        _validated_trusted_proof,
    )
    from codey.workspace.revision import WorkspaceIdentity

    identity = WorkspaceIdentity.trusted_pair(2, "sha256:" + "ab" * 32)

    class DerivedProof(TrustedWorkspaceProof):
        pass

    class DerivedIdentity(WorkspaceIdentity):
        pass

    valid = _trusted_workspace_proof(identity, "event_side_channel")
    derived = object.__new__(DerivedProof)
    for field in ("identity", "source", "_capability"):
        object.__setattr__(derived, field, getattr(valid, field))
    for proof in (
        derived,
        _trusted_workspace_proof(identity, 123),
        _trusted_workspace_proof(DerivedIdentity(identity.revision, identity.fingerprint), "event_side_channel"),
    ):
        with pytest.raises(RecoveryFailed):
            _validated_trusted_proof(proof)


def test_projection_failure_callback_error_preserves_recovery_failure() -> None:
    from codey.operations.kernel_errors import RecoveryFailed
    from codey.operations.kernel_events import _emit_tool_results
    from codey.operations.task_session import TaskSession, turn_effect_id
    from codey.policies.task_policy import TaskPolicy
    from codey.runtime.core.models import ToolCall, ToolResult
    from codey.workspace.revision import WorkspaceIdentity

    session = TaskSession(policy=TaskPolicy(grants=frozenset({"project.write", "control"})))
    call = ToolCall(name="edit", args={"path": "a.py"}, call_id="c1")
    session.executed[turn_effect_id("run", 1, 0)] = {"ok": True}
    result = ToolResult(call=call, model_text="edited", audit={"changed": True})
    object.__setattr__(result, "_kernel_workspace_identity", WorkspaceIdentity.trusted_pair(2, "sha256:" + "ab" * 32))

    def callback(event: object) -> None:
        if event.outcome and not event.outcome.ok:  # type: ignore[attr-defined]
            raise RuntimeError("sink unavailable")

    with mock.patch(
        "codey.operations.kernel_provenance.attach_proof_to_event",
        side_effect=ValueError("bad proof"),
    ), pytest.raises(RecoveryFailed, match="event proof attach failed"):
        _emit_tool_results(callback, session, [result], run_id="run", turn=1)
