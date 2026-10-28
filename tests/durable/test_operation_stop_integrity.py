"""Stopping a lifecycle cannot replace effect receipts or imply delivery."""

from pathlib import Path

import pytest

from codey.durable import DurableSession, MemoryDurableStore, SQLiteDurableStore
from codey.durable.checkpoint import checkpoint_entries, operation_from_checkpoint
from codey.durable.errors import CommitConflict
from codey.runtime.core.operation_state import RuntimeOperationTransitionError, mark_terminal
from codey.runtime.effects.effect_records import RuntimeEffectIntent, RuntimeEffectSettlement
from codey.runtime.effects.tool_result_delivery import DeliveryBatchIntent, DeliveryBatchItem, compute_batch_digest
from codey.runtime.log.session_view import load_session_view
from codey.runtime.write.mutation_line import RuntimeMutationLine


@pytest.fixture(params=["memory", "sqlite"])
def runtime(request, tmp_path: Path):
    store = MemoryDurableStore() if request.param == "memory" else SQLiteDurableStore(tmp_path / "runtime.sqlite")
    durable = DurableSession(store)
    durable.admit_input(conversation_id="s", request_id="input", task_id="r", text="inspect", provider_snapshot={})
    durable.start_task("r", state={})
    mutations = RuntimeMutationLine(durable=durable)
    mutations.accept_operation(session_id="s", run_id="r", provider_id="local", turn_budget=5,
                               max_repair_rounds=1, task_kind="project")
    mutations.mark_writer_running("s", "r", provider_id="local")
    try:
        yield durable, mutations
    finally:
        durable.close()


def _repair(mutations):
    mutations.mark_writer_settled("s", "r", provider_id="local", turns_used=1, stop_reason="done")
    mutations.record_completion_proof("s", "r", proof_ref="completion_proof:" + "a" * 16, proof_status="failed")
    mutations.admit_repair_context("s", "r", context_ref="sha256:" + "b" * 64)
    mutations.mark_repair_running("s", "r", provider_id="local")


def _stop(mutations, driver, terminal):
    if terminal:
        return mutations.mark_terminal("s", "r", stop_reason="error", summary_chars=4, turns=1,
                                       max_turns=5, provider="local")
    if driver == "repair":
        return mutations.mark_repair_settled("s", "r", provider_id="local", turns_used=1, stop_reason="error")
    return mutations.mark_writer_settled("s", "r", provider_id="local", turns_used=1, stop_reason="error")


def _tools(mutations, driver):
    items = (DeliveryBatchItem(0, "read", "tool", "safe"),)
    mutations.begin_tool_batch("s", "r", intents=(RuntimeEffectIntent(
        effect_id="tool", effect_category="tool_call", session_id="s", run_id="r", phase=driver,
        turn=1, tool_index=0, tool_name="read", args_digest="read-digest", replay_class="safe",
        delivery_batch_id="batch", replay_args={"path": "a.py"},
    ),), delivery_intent=DeliveryBatchIntent(batch_id="batch", session_id="s", run_id="r", turn=1,
        items=items, batch_digest=compute_batch_digest(items)))


@pytest.mark.parametrize("driver", ["writer", "repair"])
@pytest.mark.parametrize("terminal", [False, True])
@pytest.mark.parametrize("category", ["provider_send", "tool_call"])
def test_pending_effect_blocks_lifecycle_stop_without_changing_facts(runtime, driver, terminal, category):
    durable, mutations = runtime
    if driver == "repair":
        _repair(mutations)
    if category == "tool_call":
        _tools(mutations, driver)
    else:
        mutations.begin_provider_effect("s", "r", RuntimeEffectIntent(
            effect_id="provider", effect_category=category, session_id="s", run_id="r", phase=driver,
            provider_id="local", turn=1, args_digest="request-digest"))
    before, sequence = durable.task_facts("r"), durable.commit_sequence()
    with pytest.raises(RuntimeOperationTransitionError, match="pending effects"):
        _stop(mutations, driver, terminal)
    assert durable.task_facts("r") == before
    assert durable.commit_sequence() == sequence


@pytest.mark.parametrize("driver", ["writer", "repair"])
@pytest.mark.parametrize("terminal", [False, True])
def test_unknown_tool_outcome_blocks_stop_and_retains_delivery(runtime, driver, terminal):
    durable, mutations = runtime
    if driver == "repair":
        _repair(mutations)
    _tools(mutations, driver)
    mutations.settle_tool_effect("s", "r", RuntimeEffectSettlement(
        effect_id="tool", effect_category="tool_call", session_id="s", run_id="r", status="interrupted",
        error_code="interrupted_by_crash", replay_class="safe"))
    before, sequence = durable.task_facts("r"), durable.commit_sequence()
    with pytest.raises(RuntimeOperationTransitionError, match="unknown external outcomes"):
        _stop(mutations, driver, terminal)
    assert durable.task_facts("r") == before
    assert durable.commit_sequence() == sequence


@pytest.mark.parametrize("driver", ["writer", "repair"])
@pytest.mark.parametrize("terminal", [False, True])
def test_known_failed_result_can_be_abandoned_without_becoming_delivered(runtime, driver, terminal):
    durable, mutations = runtime
    if driver == "repair":
        _repair(mutations)
    _tools(mutations, driver)
    mutations.settle_tool_effect("s", "r", RuntimeEffectSettlement(
        effect_id="tool", effect_category="tool_call", session_id="s", run_id="r", status="error",
        error_code="file_not_found", replay_class="safe"))
    effects, sequence = durable.effects("r"), durable.commit_sequence()
    _stop(mutations, driver, terminal)
    assert durable.commit_sequence() == sequence + 1
    assert durable.effects("r") == effects
    task, effects = durable.task_facts("r")
    view = load_session_view(checkpoint_entries(task, effects, session_id="s"), session_id="s", run_id="r")
    [batch] = view.batches
    assert batch.is_abandoned and not batch.is_delivered
    assert batch.delivered_effect_ids == ()
    assert batch.abandoned_reason == ("operation_terminal" if terminal else f"{driver}_settled")


def test_terminal_commit_retry_after_success_does_not_duplicate_abandonment(runtime, monkeypatch):
    durable, mutations = runtime
    _tools(mutations, "writer")
    mutations.settle_tool_effect("s", "r", RuntimeEffectSettlement(
        effect_id="tool", effect_category="tool_call", session_id="s", run_id="r", replay_class="safe"))
    sequence = durable.commit_sequence()
    commit = durable.store.commit
    interrupted = False

    def lost_success(mutation):
        nonlocal interrupted
        result = commit(mutation)
        if not interrupted:
            interrupted = True
            raise CommitConflict("terminal committed before caller observed success")
        return result

    monkeypatch.setattr(durable.store, "commit", lost_success)
    _stop(mutations, "writer", True)
    assert interrupted
    assert durable.commit_sequence() == sequence + 1
    assert operation_from_checkpoint(durable.task("r"), "s").leaf == "terminal"


def test_scheduler_failure_cannot_replace_authoritative_runtime_facts(runtime):
    durable, mutations = runtime
    mutations.begin_provider_effect("s", "r", RuntimeEffectIntent(
        effect_id="provider", effect_category="provider_send", session_id="s", run_id="r", phase="writer",
        provider_id="local", turn=1, args_digest="request-digest"))
    before = durable.task("r")
    with pytest.raises(CommitConflict):
        # A terminal caller cannot erase the authoritative pending index.
        durable.fail_task("r", "controller failure", state={"pending_effect_ids": [], "operation": {}})
    assert durable.task("r") == before


def test_repeated_terminal_call_does_not_accept_a_corrupt_pending_ledger(runtime):
    durable, mutations = runtime
    _tools(mutations, "writer")
    task = durable.task("r")
    state = operation_from_checkpoint(task, "s")
    corrupt_terminal = mark_terminal(state, stop_reason="error", summary_chars=4, turns=1,
                                     max_turns=5, provider="local")
    # Simulate contradictory data from an older writer. The idempotent
    # terminal fast path must check the ledger, rather than bless this state.
    durable.advance_task(task_id="r", phase="running",
                         state={**task.state, "operation": corrupt_terminal.to_payload()})
    before, sequence = durable.task_facts("r"), durable.commit_sequence()
    with pytest.raises(RuntimeOperationTransitionError, match="pending effects"):
        _stop(mutations, "writer", True)
    assert durable.task_facts("r") == before
    assert durable.commit_sequence() == sequence
