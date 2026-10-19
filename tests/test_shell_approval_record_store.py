from __future__ import annotations

from codey.app.context import AppContext
from codey.runtime.effects.shell_approval_records import ShellApprovalFact, ShellApprovalRecordStore
from tests.support.shell_approval import admit_shell_task


def test_shell_approval_facts_survive_store_reopen(tmp_path) -> None:
    fact = ShellApprovalFact("a1", "s1", "r1", "requested", command_ref="artifact:cmd", command_digest="d" * 64)
    store = ShellApprovalRecordStore(tmp_path)
    store.append(fact)
    reopened = ShellApprovalRecordStore(tmp_path)
    assert reopened.latest("a1") == fact


def test_shell_approval_latest_fact_tracks_settlement(tmp_path) -> None:
    store = ShellApprovalRecordStore(tmp_path)
    store.append(ShellApprovalFact("a1", "s1", "r1", "requested"))
    settled = ShellApprovalFact("a1", "s1", "r1", "execution_settled", result_ref="artifact:result")
    store.append(settled)
    assert store.latest("a1") == settled


def test_cold_start_approval_rebuild_restores_current_command_fields(tmp_path) -> None:
    state = AppContext(tmp_path)
    try:
        admit_shell_task(state, session_id="s", run_id="r", task="shell approval")
        artifact = state.managed_outputs.write_json_artifact(
            session_id="s", run_id="r", payload={"command": "echo hello", "cwd": "."}
        )
        state.record_shell_approval_fact(ShellApprovalFact(
            "a1", "s", "r", "requested", command_ref=artifact.handle,
            command_digest=artifact.sha256, cwd=".",
        ))
        pending = state.claim_pending_shell_approval("a1", claim_execution=False)
        assert pending is not None
        assert pending["command_preview"] == "echo hello"
        assert pending["command_chars"] == len("echo hello")
        assert pending["command_truncated"] is False
        assert len(pending["command_sha256"]) == 64
    finally:
        state.close()
