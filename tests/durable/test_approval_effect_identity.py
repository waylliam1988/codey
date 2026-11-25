from dataclasses import replace
from pathlib import Path

import pytest

from codey.durable import ApprovalRecord, ApprovalStatus, DurableSession, EffectRecord, SQLiteDurableStore
from tests.support.sqlite_store import TemporarySQLiteStore


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_settlement_uses_the_claimed_effect_identity(backend: str, tmp_path: Path) -> None:
    store = TemporarySQLiteStore() if backend == "memory" else SQLiteDurableStore(tmp_path / "durable.sqlite3")
    session = DurableSession(store)
    try:
        session.admit_input(
            conversation_id="chat", request_id="request", task_id="task", text="work", provider_snapshot={},
        )
        session.advance_task(task_id="task", phase="running", state={})
        requested = ApprovalRecord(
            "approval", "task", "chat", ApprovalStatus.REQUESTED, "sha256:command", "artifact:command", ".",
        )
        session.create_approval(requested)
        session.transition_approval(
            "approval", replace(requested, status=ApprovalStatus.APPROVED, revision=1),
            expected_status=ApprovalStatus.REQUESTED,
        )
        effect = EffectRecord(
            "recorded-effect", "task", "shell", "unsafe", "sha256:args", "pending", {"approval_id": "approval"},
        )
        assert session.claim_approval_and_begin_effect("approval", effect) is not None
        before = session.commit_sequence()

        settlement: dict[str, object] = {"result_ref": "result", "result_digest": "digest"}
        assert session.settle_approval("approval", settlement=settlement, resume=True) is not None

        assert session.commit_sequence() == before + 1
        assert session.approval("approval").status is ApprovalStatus.EXECUTION_SETTLED
        assert session.effect("recorded-effect").settlement == settlement
        assert session.task("task").pending_effect_id is None
        assert session.settle_approval("approval", settlement=settlement, resume=True) is None
    finally:
        session.close()
