"""A task decision must retain its full task-only artifact ownership."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Literal

import pytest

from codey.app.memory_candidates import accepted_completion, accepted_done_decision
from codey.durable import DurableSession, SQLiteDurableStore
from codey.durable.model import EffectRecord
from codey.operations.kernel_contracts import DoneDecision, KernelResult
from codey.runtime.core.operation_payload import RuntimeOperationState
from codey.runtime.core.operation_state import lane_for_run, operation_id_for_run
from codey.storage.managed_outputs import ManagedOutputStore
from tests.support.sqlite_store import TemporarySQLiteStore

NOW = "2026-11-24T00:00:00Z"


@pytest.fixture(params=("memory", "sqlite"))
def storage(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[tuple[DurableSession, ManagedOutputStore]]:
    backend: object = request.param
    assert backend in ("memory", "sqlite")
    store = SQLiteDurableStore(tmp_path / "durable.sqlite3") if backend == "sqlite" else TemporarySQLiteStore()
    durable = DurableSession(store)
    try:
        yield durable, ManagedOutputStore(tmp_path)
    finally:
        durable.close()


def _decision_checkpoint(
    durable: DurableSession, outputs: ManagedOutputStore, *, effect_owned: bool,
) -> tuple[DoneDecision, str]:
    durable.admit_input(conversation_id="chat", request_id="input", task_id="run", text="fixture",
                        provider_snapshot={"provider_id": "local"})
    durable.start_task("run", state={})
    durable.advance_task(task_id="run", phase="running", state={})
    if effect_owned:
        durable.record_effect_intent("run", EffectRecord("effect", "run", "tool_call", "safe", "digest", "pending", {}))
    decision = DoneDecision(
        purpose="done", result=KernelResult(True, "accepted", 1, "done").to_payload(),
        workspace_revision=1, workspace_fingerprint="revision-1",
    )
    payload: dict[str, object] = {"schema_version": 2, "kind": "kernel_decision", "data": decision}
    ref = outputs.write_json_artifact(session_id="chat", run_id="run", payload=payload)
    artifact = outputs.json_artifact_record(
        ref, owner_task_id="run", owner_effect_id="effect" if effect_owned else None,
        metadata={"kind": "kernel_decision"},
    )
    durable.register_artifacts("run", (artifact,))
    state = RuntimeOperationState(
        session_id="chat", run_id="run", operation_id=operation_id_for_run("run"), lane=lane_for_run("run"), project_ref="",
        provider_id="local", turn_budget=4, max_repair_rounds=1, leaf="kernel_settled",
        started_at=NOW, updated_at=NOW, work_ref=artifact.handle, driver="writer",
    )
    durable.advance_task(task_id="run", phase="running", state={"operation": state.to_payload()})
    return decision, artifact.handle


def test_task_owned_decision_retains_its_original_result(
    storage: tuple[DurableSession, ManagedOutputStore],
) -> None:
    durable, outputs = storage
    expected, handle = _decision_checkpoint(durable, outputs, effect_owned=False)
    decision, reference = accepted_done_decision(durable, outputs, "run")
    assert decision == expected
    assert reference == handle
    result, result_reference = accepted_completion(durable, outputs, "run")
    assert result is not None and result["summary"] == "accepted"
    assert result_reference == handle


@pytest.mark.parametrize("reader", ["decision", "completion"])
def test_effect_owned_bytes_cannot_be_accepted_as_a_task_decision(
    storage: tuple[DurableSession, ManagedOutputStore], reader: Literal["decision", "completion"],
) -> None:
    durable, outputs = storage
    _decision_checkpoint(durable, outputs, effect_owned=True)
    # The producer's task decision channel is (task_id, no effect_id). Sharing
    # a task ID does not make an effect-owned artifact a task-owned decision.
    with pytest.raises(ValueError, match="decision owner"):
        if reader == "decision":
            accepted_done_decision(durable, outputs, "run")
        else:
            accepted_completion(durable, outputs, "run")
