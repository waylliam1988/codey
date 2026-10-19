from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from codey.ghost.affinity import GhostAffinityStore
from codey.ghost.hebbian import GhostHebbianStore
from codey.ghost.inbox import GhostInboxStore
from tests.test_ghost_affinity import _accepted_candidate


def test_learning_graphs_are_relational_and_reopen_without_json_projections(tmp_path: Path) -> None:
    inbox = GhostInboxStore(tmp_path)
    candidate = _accepted_candidate(inbox)
    hebbian = GhostHebbianStore(tmp_path)
    assert hebbian.reinforce_candidate(candidate).applied
    affinity = GhostAffinityStore(tmp_path)
    assert affinity.sync_from_sources(hebbian_store=hebbian).ok
    assert len(hebbian.list_nodes()) == 1
    assert affinity.list_nodes()
    assert not (tmp_path / "ghost" / "state.json").exists()
    assert not (tmp_path / "ghost" / "affinity.json").exists()
    assert not (tmp_path / "ghost" / "hebbian_events.jsonl").exists()
    assert not (tmp_path / "ghost" / "affinity_events.jsonl").exists()
    with sqlite3.connect(tmp_path / "ghost.sqlite3") as database:
        assert database.execute("SELECT count(*) FROM graph_nodes WHERE graph='hebbian'").fetchone()[0] == 1
        assert database.execute("SELECT count(*) FROM graph_events").fetchone()[0] > 0
        assert database.execute("SELECT count(*) FROM graph_consumers WHERE applied_sequence>0").fetchone()[0] == 2
    reopened = GhostHebbianStore(tmp_path)
    assert reopened.list_nodes() == hebbian.list_nodes()
    assert not reopened.reinforce_candidate(candidate).applied
    assert GhostAffinityStore(tmp_path).list_nodes() == affinity.list_nodes()


def test_graph_event_and_memory_update_roll_back_together(tmp_path: Path, monkeypatch) -> None:
    inbox = GhostInboxStore(tmp_path)
    candidate = _accepted_candidate(inbox)
    hebbian = GhostHebbianStore(tmp_path)
    assert hebbian.reinforce_candidate(candidate).applied
    affinity = GhostAffinityStore(tmp_path)
    before = affinity.export_state()

    def fail(*_args, **_kwargs):
        raise OSError("injected graph write failure")

    monkeypatch.setattr(affinity, "_write_state", fail)
    assert not affinity.sync_from_sources(hebbian_store=hebbian).ok
    restored = GhostAffinityStore(tmp_path)
    assert restored.list_nodes() == ()
    assert restored.export_state()["affinity_events"] == before["affinity_events"]


@pytest.mark.parametrize("graph", ["hebbian", "affinity"])
def test_graph_transactions_do_not_lock_the_task_database(tmp_path: Path, graph: str) -> None:
    from codey.durable import DurableSession, SQLiteDurableStore

    store = GhostHebbianStore(tmp_path) if graph == "hebbian" else GhostAffinityStore(tmp_path)
    durable = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    try:
        with store.graph.transaction():
            durable.admit_input(conversation_id="c", request_id="r", task_id="t", text="go", provider_snapshot={})
        assert durable.task("t").phase == "accepted"
    finally:
        durable.close()


def test_all_ghost_stores_share_the_independent_sqlite_owner(tmp_path: Path) -> None:
    from codey.ghost.continuity import GhostContinuityStore
    from codey.ghost.sleep import GhostSleepStore
    from codey.ghost.work_queue import GhostWorkQueueStore
    from tests.test_ghost_affinity import _candidate

    inbox = GhostInboxStore(tmp_path)
    candidate = _accepted_candidate(inbox)
    hebbian = GhostHebbianStore(tmp_path)
    assert hebbian.reinforce_candidate(candidate).applied
    affinity = GhostAffinityStore(tmp_path)
    continuity = GhostContinuityStore(tmp_path)
    queue = GhostWorkQueueStore(tmp_path)
    assert queue.sync_from_sources(research_interest_candidates=(_candidate(),)).ok
    sleep = GhostSleepStore(tmp_path)
    report = sleep.run_once(
        inbox_store=inbox,
        hebbian_store=hebbian,
        affinity_store=affinity,
        continuity_store=continuity,
        work_queue_store=queue,
        session_id="s1",
    )
    assert all(step.ok for step in report.steps), report
    assert GhostInboxStore(tmp_path).list_candidates() == inbox.list_candidates()
    assert GhostWorkQueueStore(tmp_path).list_items() == queue.list_items()
    assert GhostSleepStore(tmp_path).export_state()["sleep_events"] == sleep.export_state()["sleep_events"]
    inbox.set_learning_enabled(False)
    assert GhostInboxStore(tmp_path).learning_enabled() is False
    assert GhostInboxStore(tmp_path).list_candidates() == inbox.list_candidates()
    assert all(step.ok for step in sleep.run_once().steps)
    assert list((tmp_path / "ghost").glob("*.json*")) == []


@pytest.mark.parametrize("collection", ["inbox", "continuity", "work_queue", "sleep"])
def test_record_event_state_and_cursor_roll_back_together(tmp_path: Path, monkeypatch, collection: str) -> None:
    from codey.ghost.continuity import GhostContinuityStore
    from codey.ghost.sleep import GhostSleepStore
    from codey.ghost.work_queue import GhostWorkQueueStore
    from tests.test_ghost_affinity import _candidate

    stores = {
        "inbox": GhostInboxStore,
        "continuity": GhostContinuityStore,
        "work_queue": GhostWorkQueueStore,
        "sleep": GhostSleepStore,
    }
    store = stores[collection](tmp_path)
    before = store.export_state()

    def fail(*_args, **_kwargs):
        raise OSError("injected record write failure")

    monkeypatch.setattr(store, "_write_state", fail)
    if collection == "inbox":
        assert store.ingest_signals([_candidate_signal()]) == ()
    elif collection == "continuity":
        assert not store.sync_from_sources(
            user_focus_excerpt="Implement durable memory", session_id="s", mode="chat"
        ).ok
    elif collection == "work_queue":
        assert not store.sync_from_sources(research_interest_candidates=(_candidate(),)).ok
    else:
        assert not store.run_once().steps[-1].ok
    restored = stores[collection](tmp_path)
    event_key = {
        "inbox": "events",
        "continuity": "continuity_events",
        "work_queue": "work_events",
        "sleep": "sleep_events",
    }[collection]
    assert restored.export_state()[event_key] == before[event_key]
    with sqlite3.connect(tmp_path / "ghost.sqlite3") as database:
        assert (
            database.execute("SELECT count(*) FROM memory_records WHERE collection=?", (collection,)).fetchone()[0] == 0
        )
        assert (
            database.execute("SELECT count(*) FROM memory_consumers WHERE collection=?", (collection,)).fetchone()[0]
            == 0
        )


def _candidate_signal():
    from codey.ghost.schema import GhostSignal

    return GhostSignal(
        kind="style_preference",
        scope="user",
        summary="Prefer small functions",
        evidence_quote="Prefer small functions",
        confidence=0.95,
        source="manual",
    )


def test_record_compaction_preserves_state_and_advances_cursor(tmp_path: Path, monkeypatch) -> None:
    import codey.ghost.inbox as inbox_module

    inbox = GhostInboxStore(tmp_path)
    candidate = _accepted_candidate(inbox)
    monkeypatch.setattr(inbox_module, "MAX_GHOST_EVENTS", 1)
    assert inbox.compact_if_needed()["compacted"]
    assert GhostInboxStore(tmp_path).list_candidates() == (candidate,)


def test_continuity_builder_reads_sqlite_after_reopen(tmp_path: Path) -> None:
    from codey.ghost.continuity import GhostContinuityStore, build_ghost_continuity

    store = GhostContinuityStore(tmp_path)
    assert store.sync_from_sources(user_focus_excerpt="Study copper supply", session_id="s", mode="chat").items_changed
    context = build_ghost_continuity(GhostContinuityStore(tmp_path), session_id="s")
    assert "Study copper supply" in context.text


@pytest.mark.parametrize("owner", ["graph", "records"])
def test_unpaired_event_write_is_rejected_without_poisoning_store(tmp_path: Path, owner: str) -> None:
    from codey.ghost.graph_store import GhostGraph
    from codey.ghost.record_store import GhostRecords
    from codey.ghost.sqlite import GhostSQLite

    database = GhostSQLite(tmp_path)
    store = GhostGraph(database, "hebbian") if owner == "graph" else GhostRecords(database, "inbox")
    with pytest.raises((OSError, ValueError), match="(cursor|position)"):
        store.append([{"event_id": "unpaired", "type": "test"}], replace=True)
    assert store.events() == []
    assert store.rows() == (([], []) if owner == "graph" else [])


def test_affinity_reports_post_mutation_warnings(tmp_path: Path, monkeypatch) -> None:
    from tests.test_ghost_affinity import _candidate

    affinity = GhostAffinityStore(tmp_path)

    def maintenance_warning():
        affinity.last_warnings = ("maintenance_notice",)
        return {"ok": True}

    monkeypatch.setattr(affinity, "compact_if_needed", maintenance_warning)
    result = affinity.sync_from_sources(research_interest_candidates=(_candidate(),))
    assert result.ok
    assert result.warnings == ("maintenance_notice",)


def test_hebbian_failed_event_write_is_reported_without_partial_state(tmp_path: Path, monkeypatch) -> None:
    candidate = _accepted_candidate(GhostInboxStore(tmp_path))
    hebbian = GhostHebbianStore(tmp_path)

    def fail(*_args, **_kwargs):
        raise OSError("injected event failure")

    monkeypatch.setattr(hebbian.graph, "append", fail)
    result = hebbian.reinforce_candidate(candidate)
    assert not result.applied
    assert result.reason == "store_error"
    assert GhostHebbianStore(tmp_path).list_nodes() == ()


@pytest.mark.parametrize("domain", ["inbox", "continuity", "work_queue", "sleep", "hebbian", "affinity"])
def test_process_exit_between_event_and_state_rolls_back(tmp_path: Path, domain: str) -> None:
    import subprocess
    import sys

    from codey.ghost.continuity import GhostContinuityStore
    from codey.ghost.sleep import GhostSleepStore
    from codey.ghost.work_queue import GhostWorkQueueStore

    _accepted_candidate(GhostInboxStore(tmp_path))
    factories = {
        "inbox": GhostInboxStore,
        "continuity": GhostContinuityStore,
        "work_queue": GhostWorkQueueStore,
        "sleep": GhostSleepStore,
        "hebbian": GhostHebbianStore,
        "affinity": GhostAffinityStore,
    }
    owner = factories[domain](tmp_path)
    storage = owner.graph if domain in {"hebbian", "affinity"} else owner.records
    events_before, rows_before = storage.events(), storage.rows()
    script = """
import os, sys
from codey.ghost.inbox import GhostInboxStore
from codey.ghost.continuity import GhostContinuityStore
from codey.ghost.work_queue import GhostWorkQueueStore
from codey.ghost.sleep import GhostSleepStore
from codey.ghost.hebbian import GhostHebbianStore
from codey.ghost.affinity import GhostAffinityStore
from tests.test_ghost_affinity import _candidate, _signal
home, domain = sys.argv[1:]
factories = dict(inbox=GhostInboxStore, continuity=GhostContinuityStore, work_queue=GhostWorkQueueStore,
                 sleep=GhostSleepStore, hebbian=GhostHebbianStore, affinity=GhostAffinityStore)
store = factories[domain](home)
store._write_state = lambda *args, **kwargs: os._exit(23)
if domain == 'inbox':
    store.ingest_signals([_signal(conflict_key='crash_new', value_key='new')], user_text='以后先给结论')
elif domain == 'continuity':
    store.sync_from_sources(user_focus_excerpt='Study copper supply', session_id='s', mode='chat')
elif domain in {'work_queue', 'affinity'}:
    store.sync_from_sources(research_interest_candidates=(_candidate(),))
elif domain == 'hebbian':
    store.reinforce_candidate(GhostInboxStore(home).list_candidates()[0])
else:
    store.run_once()
raise SystemExit(99)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), domain], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 23, result.stderr
    reopened = factories[domain](tmp_path)
    restored = reopened.graph if domain in {"hebbian", "affinity"} else reopened.records
    assert restored.events() == events_before
    assert restored.rows() == rows_before


def test_sleep_detects_a_missing_consumer_cursor(tmp_path: Path) -> None:
    from codey.ghost.sleep import GhostSleepStore

    _accepted_candidate(GhostInboxStore(tmp_path))
    with sqlite3.connect(tmp_path / "ghost.sqlite3") as database:
        database.execute("DELETE FROM memory_consumers WHERE collection='inbox'")
    report = GhostSleepStore(tmp_path).run_once()
    health = next(step for step in report.steps if step.name == "projection_health")
    assert not health.ok
    assert health.counts["invalid_cursors"] == 1


def test_learning_switch_failure_rolls_back_both_setting_and_event(tmp_path: Path, monkeypatch) -> None:
    inbox = GhostInboxStore(tmp_path)

    def fail(*_args, **_kwargs):
        raise OSError("injected settings event failure")

    monkeypatch.setattr(inbox.records, "append", fail)
    assert not inbox.set_learning_enabled(False)
    assert GhostInboxStore(tmp_path).learning_enabled()
    assert inbox.records.events() == []


def test_sleep_does_not_report_success_when_affinity_cannot_decay(tmp_path: Path, monkeypatch) -> None:
    from codey.ghost.sleep import GhostSleepStore

    affinity = GhostAffinityStore(tmp_path)

    def fail(*_args, **_kwargs):
        raise OSError("injected affinity read failure")

    monkeypatch.setattr(affinity.graph, "rows", fail)
    report = GhostSleepStore(tmp_path).run_once(affinity_store=affinity)
    decay = next(step for step in report.steps if step.name == "affinity_decay")
    assert not decay.ok


def test_affinity_dictionary_result_propagates_warnings(tmp_path: Path, monkeypatch) -> None:
    from tests.test_ghost_affinity import _candidate

    affinity = GhostAffinityStore(tmp_path)
    assert affinity.sync_from_sources(research_interest_candidates=(_candidate(),), session_id="s1").ok

    def warning():
        affinity.last_warnings = ("maintenance_notice",)
        return {"ok": True}

    monkeypatch.setattr(affinity, "compact_if_needed", warning)
    assert affinity.delete_scope("session", session_id="s1")["warnings"] == ["maintenance_notice"]
