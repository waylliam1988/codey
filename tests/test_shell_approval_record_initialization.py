"""The approval JSONL is an optional audit export, never a startup owner."""

import json
from dataclasses import replace

import pytest

from codey.agents.shell_approval import ShellApprovalRequest
from codey.app.context import AppContext
from codey.app.shell_approval_state import restore_pending_shell_approvals
from codey.app.shell_approval_workflow import _CONTINUATION_STATES
from codey.runtime.effects import shell_approval_records as records


def test_each_continuation_fact_has_an_explicit_status_projection() -> None:
    continuation_facts = {kind for kind in records.FACT_KINDS if kind.startswith("continuation_")}
    assert continuation_facts == set(_CONTINUATION_STATES)


def test_audit_store_round_trip_does_not_claim_lifecycle_ownership(tmp_path) -> None:
    requested = records.ShellApprovalFact("approval", "chat", "run", "requested")
    approved = replace(requested, kind="approved")
    store = records.ShellApprovalRecordStore(tmp_path)
    store.append(requested)
    store.append(approved)
    assert store.read("approval") == (requested, approved)
    assert store.latest("approval") == approved
    assert store.histories() == {"approval": (requested, approved)}


def test_audit_store_does_not_upgrade_or_rewrite_old_rows_on_startup(tmp_path) -> None:
    path = tmp_path / "runtime" / "shell_approvals.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = records.ShellApprovalFact("approval", "chat", "run", "requested").to_payload()
    payload.pop("continuation_run_id")
    original = (json.dumps(payload) + "\n").encode()
    path.write_bytes(original)
    store = records.ShellApprovalRecordStore(tmp_path)
    assert path.read_bytes() == original
    with pytest.raises(ValueError):
        store.read("approval")
    assert not list(path.parent.glob("shell_approvals.before-continuation-*.jsonl"))


def test_pending_approval_recovery_reads_durable_task_without_audit(tmp_path) -> None:
    state = AppContext(tmp_path)
    try:
        state.durable.admit_input(
            conversation_id="chat",
            request_id="run",
            task_id="run",
            text="shell request",
            provider_snapshot={"provider_id": "local"},
        )
        artifact = state.managed_outputs.write_json_artifact(
            session_id="chat", run_id="run", payload={"command": "echo hello", "cwd": ".", **ShellApprovalRequest(".", "echo hello").to_payload()}
        )
        state.record_shell_approval_fact(records.ShellApprovalFact(
            "approval", "chat", "run", "requested",
            command_ref=artifact.handle, command_digest=artifact.sha256, cwd=".",
        ))
        state.approvals.pop_shell("approval")
        state.shell_approval_records.path.unlink()
        assert restore_pending_shell_approvals(state) == 1
        assert state.approvals.shell_snapshot()["approval"]["command"] == "echo hello"
    finally:
        state.close()
