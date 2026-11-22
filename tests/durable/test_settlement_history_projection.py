"""Settlement projection retains the original history boundary and scope."""

from dataclasses import replace
from pathlib import Path

import pytest

from codey.durable import DurableSession, EffectRecord, MemoryDurableStore, SQLiteDurableStore, TaskCheckpoint
from codey.durable.model import DurableMutation
from codey.durable.timeline import TaskSettlementHistory, execution_payload, settlement_histories


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_projection_ignores_empty_workflows_and_excludes_later_changes(backend: str, tmp_path: Path) -> None:
    store = MemoryDurableStore() if backend == "memory" else SQLiteDurableStore(tmp_path / "durable.sqlite3")
    durable = DurableSession(store)
    try:
        admitted = durable.admit_input(conversation_id="c", request_id="r", task_id="t", text="go", provider_snapshot={})
        durable.start_task("t", state={})
        durable.advance_task(task_id="t", phase="running", state={})
        intent = EffectRecord("e", "t", "tool_call", "unsafe", "digest", "pending", {"tool": "run"})
        pending = durable.record_effect_intent("t", intent)
        pending_task = durable.task("t")
        effect = replace(intent, status="ok", settlement={"exit_code": 0})
        settled = durable.settle_effect("t", effect)
        settled_task = durable.task("t")
        durable.update_task_state("t", {"step": "later"})
        # Passive messages legitimately produce _workflow={}, not a task record.
        durable.record_display_message("c", run_id="t", identity="passive", payload={"text": "done"})

        assert store.task_settlement_history(("t", "missing"), through=admitted.sequence - 1) == ()
        assert store.task_settlement_history(("t",), through=pending.sequence) == (
            TaskSettlementHistory(pending_task, ()),
        )
        assert store.task_settlement_history(("t",), through=settled.sequence) == (
            TaskSettlementHistory(settled_task, ((settled.sequence, effect),)),
        )
        assert durable.settled_effects("t") == (effect,)
        assert store.task_settlement_history(("t",), through=store.commit_sequence())[0].task.state["step"] == "later"
        with pytest.raises(KeyError, match="no checkpoint"):
            durable.settled_effects("missing")
        if backend == "sqlite":
            durable.close()
            durable = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
            assert durable.settled_effects("t") == (effect,)
            assert durable.store.task_settlement_history(("t",), through=settled.sequence) == (
                TaskSettlementHistory(settled_task, ((settled.sequence, effect),)),
            )
    finally:
        durable.close()


@pytest.mark.parametrize("legacy", [False, True])
def test_projection_selects_its_task_and_retains_each_terminal_occurrence(legacy: bool) -> None:
    task = TaskCheckpoint("t", "c", "s", "running", 0, {}, {})
    other = replace(task, task_id="other")
    effect = EffectRecord("e", "t", "tool_call", "unsafe", "digest", "ok", {}, {"exit_code": 0})
    unrelated = replace(effect, effect_id="unrelated", task_id="other")
    mutation = DurableMutation("c", 0, task=task, task_updates=(other,), effects=(effect,))
    payload = execution_payload(mutation, None)
    if legacy:
        payload.pop("tasks")
    unrelated_payload = execution_payload(DurableMutation("c", 0, task=other, effects=(unrelated,)), None)
    rows: list[tuple[int, object]] = [(0, unrelated_payload), (1, {}), (2, payload), (3, payload)]

    histories = settlement_histories(("t", "missing"), rows)

    assert histories == (TaskSettlementHistory(task, ((2, effect), (3, effect))),)
    # The caller must see a later contradictory settlement, rather than losing
    # it when selecting only the latest checkpoint or deduplicating receipts.
    changed = execution_payload(replace(mutation, effects=(replace(effect, settlement={"exit_code": 1}),)), None)
    assert settlement_histories(("t",), [*rows, (4, changed)])[0].settlements[-1][1].settlement == {"exit_code": 1}


@pytest.mark.parametrize("corruption", ["schema", "effects", "effect", "intent"])
def test_projection_rejects_corrupt_relevant_history(corruption: str) -> None:
    task = TaskCheckpoint("t", "c", "s", "running", 0, {}, {})
    payload = execution_payload(DurableMutation("c", 0, task=task), None)
    if corruption == "schema":
        payload["schema_version"] = 99
    elif corruption == "effects":
        payload["effects"] = "not an array"
    elif corruption == "effect":
        payload["effects"] = [None]
    else:
        payload["effects"] = [{"task_id": "t", "settlement": {}, "intent": []}]
    with pytest.raises(ValueError):
        settlement_histories(("t",), [(1, payload)])


@pytest.mark.parametrize("conflict", [False, True])
def test_query_keeps_first_settlement_order_and_rejects_conflicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conflict: bool,
) -> None:
    store = SQLiteDurableStore(tmp_path / "durable.sqlite3")
    durable = DurableSession(store)
    try:
        durable.admit_input(conversation_id="c", request_id="r", task_id="t", text="go", provider_snapshot={})
        durable.start_task("t", state={})
        durable.advance_task(task_id="t", phase="running", state={})
        settled: list[EffectRecord] = []
        for identity in ("z-first", "a-second"):
            intent = EffectRecord(identity, "t", "tool_call", "unsafe", "digest", "pending", {})
            durable.record_effect_intent("t", intent)
            effect = replace(intent, status="ok", settlement={"exit_code": 0})
            durable.settle_effect("t", effect)
            settled.append(effect)
        read_history = store.task_settlement_history

        def repeated_history(task_ids: tuple[str, ...], *, through: int) -> tuple[TaskSettlementHistory, ...]:
            histories = read_history(task_ids, through=through)
            first = settled[0]
            duplicate = replace(first, settlement={"exit_code": 1}) if conflict else first
            return (replace(histories[0], settlements=(*histories[0].settlements, (through, duplicate))),)

        # Simulate a repeated/corrupt historical terminal occurrence, rather
        # than asking the writer to violate its immutable settlement contract.
        monkeypatch.setattr(store, "task_settlement_history", repeated_history)
        if conflict:
            with pytest.raises(ValueError, match="conflicting terminal"):
                durable.settled_effects("t")
        else:
            assert durable.settled_effects("t") == tuple(settled)
    finally:
        durable.close()
