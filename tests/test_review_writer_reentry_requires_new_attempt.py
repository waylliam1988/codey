"""A review repair reenters writing explicitly; settled/failed facts cannot leak."""
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

from codey.runtime.core.operation_state import (
    LEAF_WRITER_SETTLED,
    RuntimeOperationState,
    RuntimeOperationTransitionError,
    mark_provider_effect_pending,
    mark_writer_running,
    operation_state_from_entries,
)
from codey.runtime.log.session_log import RuntimeSessionLog
from codey.runtime.write.mutation_line import RuntimeMutationLine


def settled():
    return RuntimeOperationState(session_id="s", run_id="r", operation_id="task:" + "1" * 24,
        lane="run:" + "2" * 24, project_ref="project:" + "3" * 24, provider_id="local",
        turn_budget=8, max_repair_rounds=1, leaf=LEAF_WRITER_SETTLED,
        started_at="2026-10-04", updated_at="2026-10-04", task_kind="project",
        writer_attempt=1, turns_used=4, stop_reason="done")


def test_review_reentry_advances_attempt_and_preserves_accumulated_budget():
    state = mark_writer_running(settled(), provider_id="local", writer_attempt=2)
    assert state.leaf == "writer_running"
    assert state.writer_attempt == 2
    # A review repair may be interrupted after this durable transition. The
    # operation must retain the turns already spent by the settled writer so a
    # cold recovery cannot reopen the full budget.
    assert state.turns_used == 4 and state.stop_reason == ""
    assert RuntimeOperationState.from_payload(state.to_payload()) == state
    pending = mark_provider_effect_pending(state, driver="writer", provider_id="local", turn=5)
    assert pending.leaf == "provider_effect_pending"


def test_interrupted_review_reentry_persists_accumulated_budget_for_cold_recovery():
    with tempfile.TemporaryDirectory() as td:
        log = RuntimeSessionLog(Path(td))
        line = RuntimeMutationLine(log)
        line.accept_operation(session_id="s", run_id="r", project="", provider_id="local", turn_budget=8,
                              max_repair_rounds=1, task_kind="project")
        line.mark_writer_running("s", "r", provider_id="local")
        line.mark_writer_settled("s", "r", provider_id="local", turns_used=4, stop_reason="done")
        running = line.mark_writer_running("s", "r", provider_id="local", writer_attempt=2)
        assert running is not None and running.turns_used == 4
        restored = operation_state_from_entries(log.read("s"), session_id="s", run_id="r")
        assert restored is not None
        assert restored.leaf == "writer_running"
        assert restored.turns_used == 4


@pytest.mark.parametrize("previous,attempt", [
    (settled(), 1), (replace(settled(), stop_reason="provider_failure"), 2),
    (replace(settled(), completion_proof_ref="completion_proof:" + "1" * 16,
             completion_proof_status="complete"), 2),
])
def test_review_reentry_rejects_reused_attempt_failure_or_final_verdict(previous, attempt):
    with pytest.raises(RuntimeOperationTransitionError):
        mark_writer_running(previous, provider_id="local", writer_attempt=attempt)
