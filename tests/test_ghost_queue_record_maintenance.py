"""Actual queue maintenance must honor records outside the triggering scope."""
from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Mapping
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from codey.app.context import AppContext
from codey.ghost.inbox import GhostInboxStore, GhostMaintenancePolicy, GhostSettingsError
from codey.ghost.record_store import GhostRecords
from codey.ghost.sleep import GhostSleepStore
from codey.ghost.sqlite import GhostSQLite
from codey.ghost.work_queue import GhostWorkQueueStore
from codey.ghost.work_queue_events import _observed_event
from codey.ghost.work_queue_model import MAX_WORK_ITEMS, MAX_WORK_RETRIES, GhostWorkItem
from codey.storage.local_store import session_key
from tests.test_ghost_maintenance_record_scope import BASE, LEASE, Scope
from tests.test_ghost_maintenance_revocation import Barrier, _report

HORIZON = "2026-11-18T00:00:00Z"


def _scope(home: Path, name: str, label: str) -> Scope:
    return Scope(name, str((home / f"{label}-project").resolve()) if name == "project"
                 else f"{label}-session" if name == "session" else "")


def _item(scope: Scope, label: str) -> GhostWorkItem:
    return GhostWorkItem(
        id=f"record-work-{label}", kind="research", status="running", scope=scope.name,
        scope_ref=session_key(scope.raw_ref) if scope.name == "session" else scope.raw_ref,
        title="Review saved follow-up",
        why_now="Persisted lease", priority=0.8, confidence=0.9, source="user",
        source_ref=f"record-source-{label}", started_run_id=f"record-run-{label}",
        retry_count=1, lease_expires_at=LEASE, created_at=BASE, updated_at=BASE,
    )


def _seed(home: Path, items: Iterable[GhostWorkItem]) -> GhostWorkQueueStore:
    rows = tuple(items)
    queue = GhostWorkQueueStore(home)
    with queue.records.transaction():
        queue.records.append(_observed_event(item, ts=BASE) for item in rows)
        queue.records.replace_rows(item.to_payload() for item in rows)
    stored = queue.records.rows()
    assert len(stored) == len(rows) and all(GhostWorkItem.from_payload(row) is not None for row in stored)
    return queue


def _switch(inbox: GhostInboxStore, scope: Scope, enabled: bool, control: str = "run_maintenance") -> None:
    assert inbox.set_control(control, enabled, scope=scope.name, project=scope.project, session_id=scope.session)


def _rows(queue: GhostWorkQueueStore) -> dict[str, dict[str, object]]:
    rows = queue.records.rows()
    return {identity: row for row in rows if isinstance(identity := row.get("id"), str)}


@pytest.mark.parametrize("target_scope", ("project", "session"))
@pytest.mark.parametrize("trigger_scope", ("user", "project", "session"))
@pytest.mark.parametrize("revoked", (False, True))
def test_normal_app_queue_respects_other_record_scope(
    tmp_path: Path, target_scope: str, trigger_scope: str, revoked: bool,
) -> None:
    target, peer = _scope(tmp_path, target_scope, "target"), _scope(tmp_path, target_scope, "peer")
    trigger = _scope(tmp_path, trigger_scope, "peer")
    target_item, peer_item = _item(target, "target"), _item(peer, "peer")
    with closing(AppContext(tmp_path)) as app:
        queue = _seed(tmp_path, (target_item, peer_item))
        inbox = GhostInboxStore(tmp_path)
        app.ghost_inbox, app.ghost_work_queue = inbox, queue
        _switch(inbox, target, not revoked)
        assert inbox.control_enabled("run_maintenance", project=trigger.project, session_id=trigger.session)
        before_events = queue.records.events()
        assert app.kick_ghost_sleep(project=trigger.project, session_id=trigger.session)
        assert app.wait_for_ghost_sleep(15)
        assert app.ghost_sleep_daemon.error_count == 0, app.ghost_sleep_daemon.last_error
        report = _report(GhostSleepStore(tmp_path))
        assert not report.cancelled and not report.warnings and all(step.ok for step in report.steps)
        rows = _rows(queue)
        assert rows[peer_item.id]["status"] == "queued" and rows[peer_item.id] != peer_item.to_payload()
        assert rows[target_item.id]["status"] == ("running" if revoked else "queued")
        assert (rows[target_item.id] == target_item.to_payload()) == revoked
        events = queue.records.events()
        assert events[:len(before_events)] == before_events and len(events) > len(before_events)
        measurement: dict[str, object] = {
            "target_scope": target_scope, "trigger_scope": trigger_scope, "revoked": revoked,
            "target_changed": rows[target_item.id] != target_item.to_payload(),
            "peer_changed": True, "pass": True,
        }
        print("QUEUE_RECORD_SCOPE_CELL " + json.dumps(measurement, sort_keys=True), flush=True)


@pytest.mark.parametrize("scope_name", ("user", "project", "session"))
@pytest.mark.parametrize("control", ("accept_events", "retrieve_projections", "admit_hints"))
def test_unrelated_controls_do_not_disable_record_maintenance(tmp_path: Path, scope_name: str, control: str) -> None:
    scope = _scope(tmp_path, scope_name, "target")
    item = _item(scope, "target")
    queue = _seed(tmp_path, (item,))
    _switch(GhostInboxStore(tmp_path), scope, False, control)
    result = queue.reconcile_stale_claims(maintenance_as_of=HORIZON)
    assert result.ok and result.items_changed == 1
    assert _rows(queue)[item.id]["status"] == "queued"


@pytest.mark.parametrize("legacy", (False, True))
def test_global_deny_and_legacy_off_protect_all_records_then_restore(tmp_path: Path, legacy: bool) -> None:
    inbox = GhostInboxStore(tmp_path)
    items = tuple(_item(_scope(tmp_path, scope, scope), scope) for scope in ("user", "project", "session"))
    queue = _seed(tmp_path, items)
    if legacy:
        with inbox.records.transaction():
            inbox.records.set_setting("learning", {"schema_version": 1, "learning_enabled": False})
    else:
        _switch(inbox, Scope("user", ""), False)
    before = (queue.records.rows(), queue.records.events())
    assert queue.reconcile_stale_claims(maintenance_as_of=HORIZON).items_changed == 0
    assert (queue.records.rows(), queue.records.events()) == before
    _switch(inbox, Scope("user", ""), True)
    result = GhostWorkQueueStore(tmp_path).reconcile_stale_claims(maintenance_as_of=HORIZON)
    assert result.ok and result.items_changed == len(items)


def test_raw_hash_shaped_session_identity_is_not_an_encoded_control(tmp_path: Path) -> None:
    raw = "a" * 24
    target, peer = Scope("session", raw), Scope("session", session_key(raw))
    target_item, peer_item = _item(target, "target"), _item(peer, "peer")
    assert target_item.scope_ref != peer_item.scope_ref
    queue = _seed(tmp_path, (target_item, peer_item))
    _switch(GhostInboxStore(tmp_path), target, False)
    result = queue.reconcile_stale_claims(maintenance_as_of=HORIZON)
    assert result.ok and result.items_changed == 1
    rows = _rows(queue)
    assert rows[target_item.id] == target_item.to_payload()
    assert rows[peer_item.id]["status"] == "queued"


def test_project_deny_cannot_be_bypassed_by_session_with_unknown_parent(tmp_path: Path) -> None:
    target, peer = _scope(tmp_path, "project", "target"), _scope(tmp_path, "project", "peer")
    scopes = (target, peer, Scope("session", "unknown-parent"), Scope("user", ""))
    items = tuple(_item(scope, str(index)) for index, scope in enumerate(scopes))
    queue = _seed(tmp_path, items)
    _switch(GhostInboxStore(tmp_path), target, False)
    result = queue.reconcile_stale_claims(maintenance_as_of=HORIZON)
    assert result.ok and result.items_changed == 2
    rows = _rows(queue)
    for index in (0, 2):
        assert rows[items[index].id] == items[index].to_payload()
    for index in (1, 3):
        assert rows[items[index].id]["status"] == "queued"


@pytest.mark.parametrize("corruption", ("entry", "maintenance", "other_control", "read_error"))
def test_corrupt_other_scope_policy_fails_closed_then_recovers(tmp_path: Path, corruption: str) -> None:
    inbox = GhostInboxStore(tmp_path)
    item = _item(Scope("user", ""), "allowed")
    queue = _seed(tmp_path, (item,))
    bad: object = "bad-entry" if corruption == "entry" else {
        "run_maintenance" if corruption == "maintenance" else "admit_hints": "bad-value",
    }
    if corruption != "read_error":
        with inbox.records.transaction():
            inbox.records.set_setting("learning", {
                "schema_version": 1, "learning_enabled": True, "control_scopes": {"session": {"unrelated": bad}},
            })
    before = (queue.records.rows(), queue.records.events())
    if corruption == "read_error":
        with patch.object(GhostInboxStore, "maintenance_policy", side_effect=sqlite3.OperationalError("policy read failed")):
            result = queue.reconcile_stale_claims(maintenance_as_of=HORIZON)
    else:
        result = queue.reconcile_stale_claims(maintenance_as_of=HORIZON)
        with pytest.raises(GhostSettingsError):
            inbox.maintenance_policy()
    assert not result.ok and result.items_changed == 0
    assert (queue.records.rows(), queue.records.events()) == before
    with inbox.records.transaction():
        inbox.records.set_setting("learning", {"schema_version": 1, "learning_enabled": True})
    result = queue.reconcile_stale_claims(maintenance_as_of=HORIZON)
    assert result.ok and result.items_changed == 1


@pytest.mark.parametrize("scope_name", ("project", "session"))
@pytest.mark.parametrize("release_control", ("reenable", "delete_control"))
def test_rebuild_restart_and_explicit_delete_do_not_reinterpret_maintenance(
    tmp_path: Path, scope_name: str, release_control: str,
) -> None:
    target, peer = _scope(tmp_path, scope_name, "target"), _scope(tmp_path, scope_name, "peer")
    target_item, peer_item = _item(target, "target"), _item(peer, "peer")
    queue = _seed(tmp_path, (target_item, peer_item))
    inbox = GhostInboxStore(tmp_path)
    _switch(inbox, target, False)
    assert queue.reconcile_stale_claims(maintenance_as_of=HORIZON).items_changed == 1
    actual_rows, actual_events = _rows(queue), queue.records.events()
    _switch(inbox, target, True)
    cold = GhostWorkQueueStore(tmp_path)
    assert cold.rebuild_from_events() and _rows(cold) == actual_rows
    assert cold.records.events() == actual_events
    # The current control cannot retroactively reclaim the denied historical lease.
    assert cold.reconcile_stale_claims(maintenance_as_of=HORIZON).items_changed == 1
    _switch(inbox, target, False)
    assert cold.delete_scope(target.name, project=target.project, session_id=target.session)["removed"] == 1
    assert cold.rebuild_from_events() and target_item.id not in _rows(cold)
    if release_control == "reenable":
        _switch(inbox, target, True)
    else:
        inbox.delete_scope(target.name, project=target.project, session_id=target.session)
    assert cold.rebuild_from_events() and target_item.id not in _rows(cold)
    assert cold.reconcile_stale_claims(maintenance_as_of=HORIZON).items_changed == 0
    assert _rows(cold)[peer_item.id]["status"] == "queued"


@pytest.mark.parametrize("scope_name", ("project", "session"))
def test_due_future_retry_limit_and_full_capacity_preserve_denied_rows(tmp_path: Path, scope_name: str) -> None:
    target, peer = _scope(tmp_path, scope_name, "target"), _scope(tmp_path, scope_name, "peer")
    protected = tuple(replace(_item(target, f"protected-{index}"), priority=0.01) for index in range(MAX_WORK_ITEMS - 3))
    due = replace(_item(peer, "due"), retry_count=MAX_WORK_RETRIES)
    future = replace(_item(peer, "future"), lease_expires_at="2026-12-01T00:00:00Z")
    expired = replace(_item(peer, "expired"), expires_at=LEASE)
    queue = _seed(tmp_path, (*protected, due, future, expired))
    assert expired.id not in {item.id for item in queue.list_items()}, "reader must hide the expired persisted item"
    _switch(GhostInboxStore(tmp_path), target, False)
    result = queue.reconcile_stale_claims(maintenance_as_of=HORIZON)
    assert result.ok and result.items_changed == 2
    rows = _rows(queue)
    assert len(rows) == MAX_WORK_ITEMS
    assert all(rows[item.id] == item.to_payload() for item in protected)
    assert rows[future.id] == future.to_payload()
    assert rows[due.id]["status"] == "blocked" and rows[due.id]["blocked_reason"] == "stale_claim"
    assert rows[expired.id]["status"] == "queued"
    assert queue.rebuild_from_events() and _rows(queue) == rows


@pytest.mark.parametrize("scope_name", ("user", "project", "session"))
def test_restricted_checkpoint_is_deferred_in_both_internal_and_normal_paths(tmp_path: Path, scope_name: str) -> None:
    target = _scope(tmp_path, scope_name, "target")
    peer = _scope(tmp_path, "project", "peer")
    target_item, peer_item = _item(target, "target"), _item(peer, "peer")
    queue = _seed(tmp_path, (target_item, peer_item))
    inbox = GhostInboxStore(tmp_path)
    _switch(inbox, target, False)
    original = queue.records.events()
    with patch("codey.ghost.work_queue.MAX_WORK_EVENTS", 1):
        # Actual budget exceeded, not a compaction mock. Allowed transitions
        # continue while both explicit and mutation-internal checkpoints defer.
        result = queue.reconcile_stale_claims(maintenance_as_of=HORIZON)
        assert result.ok and result.items_changed == (0 if scope_name == "user" else 1)
        assert queue.compact_if_needed()["compacted"] is False
        current = queue.records.events()
        assert current[:len(original)] == original
        assert not any(event.get("type") == "ghost_work_snapshot" for event in current)
        rows = _rows(queue)
        assert rows[target_item.id] == target_item.to_payload()
        _switch(inbox, target, True)
        assert queue.compact_if_needed()["compacted"] is True
    compacted = queue.records.events()
    assert any(event.get("type") == "ghost_work_snapshot" for event in compacted)
    assert queue.rebuild_from_events() and _rows(queue) == rows


class FailingEventRecords(GhostRecords):
    def append(self, events: Iterable[Mapping[str, object]], *, replace: bool = False) -> None:
        super().append(events, replace=replace)
        raise sqlite3.OperationalError("failure after actual event append")


class FailingProjectionQueue(GhostWorkQueueStore):
    def _write_state(self, items: Iterable[GhostWorkItem], *, warnings: Iterable[str]) -> None:
        super()._write_state(items, warnings=warnings)
        raise sqlite3.OperationalError("failure after actual projection write")


@pytest.mark.parametrize("scope_name", ("project", "session"))
@pytest.mark.parametrize("fault", ("event", "projection"))
def test_actual_allowed_write_failure_rolls_back_with_denied_records_intact(
    tmp_path: Path, scope_name: str, fault: str,
) -> None:
    target, peer = _scope(tmp_path, scope_name, "target"), _scope(tmp_path, scope_name, "peer")
    queue = _seed(tmp_path, (_item(target, "target"), _item(peer, "peer")))
    _switch(GhostInboxStore(tmp_path), target, False)
    failing = FailingProjectionQueue(tmp_path) if fault == "projection" else GhostWorkQueueStore(tmp_path)
    if fault == "event":
        failing.records = FailingEventRecords(failing.database, "work_queue")
    before = (queue.records.rows(), queue.records.events())
    result = failing.reconcile_stale_claims(maintenance_as_of=HORIZON)
    assert not result.ok and result.items_changed == 0
    assert (queue.records.rows(), queue.records.events()) == before
    cold = GhostWorkQueueStore(tmp_path)
    assert cold.reconcile_stale_claims(maintenance_as_of=HORIZON).items_changed == 1


@pytest.mark.parametrize("scope_name", ("project", "session"))
@pytest.mark.parametrize("deny_first", (False, True))
def test_record_policy_and_transitions_are_linearized_with_concurrent_revocation(
    tmp_path: Path, scope_name: str, deny_first: bool,
) -> None:
    target, peer = _scope(tmp_path, scope_name, "target"), _scope(tmp_path, scope_name, "peer")
    target_item, peer_item = _item(target, "target"), _item(peer, "peer")
    queue = _seed(tmp_path, (target_item, peer_item))
    inbox = GhostInboxStore(tmp_path)
    barrier = Barrier()
    original_policy = GhostInboxStore.maintenance_policy
    errors: list[str] = []
    changed: list[int] = []
    committed, attempted, finished = threading.Event(), threading.Event(), threading.Event()
    committed_rows: dict[str, dict[str, object]] = {}

    def policy(owner: GhostInboxStore) -> GhostMaintenancePolicy:
        snapshot = original_policy(owner)
        barrier.pause()
        return snapshot

    def work() -> None:
        attempted.set()
        try:
            result = queue.reconcile_stale_claims(maintenance_as_of=HORIZON)
            assert result.ok, result
            changed.append(result.items_changed)
        except Exception as exc:
            errors.append(f"work {type(exc).__name__}: {exc}")
        finally:
            finished.set()

    def revoke() -> None:
        attempted.set()
        try:
            with GhostSQLite(tmp_path).write_transaction():
                _switch(inbox, target, False)
                committed_rows.update(_rows(queue))
            committed.set()
        except Exception as exc:
            errors.append(f"revoke {type(exc).__name__}: {exc}")

    worker, writer = threading.Thread(target=work, daemon=True), threading.Thread(target=revoke, daemon=True)
    if deny_first:
        with GhostSQLite(tmp_path).write_transaction():
            _switch(inbox, target, False)
            worker.start()
            assert attempted.wait(10)
            assert not finished.wait(0.2), "queue write ran through an uncommitted deny"
        worker.join(10)
        assert not worker.is_alive() and not errors
        assert changed == [1] and _rows(queue)[target_item.id] == target_item.to_payload()
    else:
        with patch.object(GhostInboxStore, "maintenance_policy", policy):
            try:
                worker.start()
                assert barrier.reached.wait(10)
                attempted.clear()
                writer.start()
                assert attempted.wait(10)
                assert not committed.wait(0.2), "deny committed after policy read but before allowed transition"
            finally:
                barrier.release.set()
            worker.join(10)
            writer.join(10)
        assert not worker.is_alive() and not writer.is_alive() and not errors, errors
        assert committed.is_set() and changed == [2] and _rows(queue) == committed_rows
    before = (queue.records.rows(), queue.records.events())
    assert GhostWorkQueueStore(tmp_path).reconcile_stale_claims(maintenance_as_of=HORIZON).items_changed == 0
    assert (queue.records.rows(), queue.records.events()) == before
