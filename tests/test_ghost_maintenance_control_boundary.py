"""Persistence, failure and transaction boundaries of normal Sleep admission."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import pytest

from codey.app.context import AppContext
from codey.ghost.affinity import GhostAffinityStore
from codey.ghost.continuity import GhostContinuityStore
from codey.ghost.inbox import GhostInboxStore
from codey.ghost.sleep import GhostSleepReport, GhostSleepStore
from codey.ghost.sqlite import GhostSQLite
from codey.ghost.work_queue import GhostWorkQueueStore
from codey.ghost.work_queue_model import GhostWorkSyncResult
from codey.storage.local_store import session_key
from tests.test_ghost_maintenance_revocation import SCOPES, Barrier, DomainStores, _report
from tests.test_ghost_sleep_clock_boundary import _seed_hebbian, _seed_queue


@dataclass(frozen=True)
class Fixture:
    home: Path
    scope: str
    project: str
    session: str
    inbox: GhostInboxStore
    stores: DomainStores

    def switch(self, name: str, enabled: bool) -> None:
        assert self.inbox.set_control(
            name, enabled, scope=self.scope, project=self.project, session_id=self.session,
        )

    def run(self, *, supply_inbox: bool = True) -> None:
        report = GhostSleepStore(self.home).run_once(
            inbox_store=self.inbox if supply_inbox else None,
            hebbian_store=self.stores.hebbian, affinity_store=self.stores.affinity,
            continuity_store=self.stores.continuity, work_queue_store=self.stores.queue,
            project=self.project, session_id=self.session,
        )
        assert report.steps[-1].ok, report.to_payload()


def _fixture(home: Path, scope: str) -> Fixture:
    project, session = str((home / "project").resolve()), "maintenance-boundary-session"
    scope_ref = project if scope == "project" else session if scope == "session" else ""
    stores = DomainStores(
        _seed_hebbian(home, scope, scope_ref), GhostAffinityStore(home), GhostContinuityStore(home),
        _seed_queue(home, scope, session_key(session) if scope == "session" else scope_ref, source_only=False),
    )
    return Fixture(home, scope, project, session, GhostInboxStore(home), stores)


@pytest.mark.parametrize("scope", SCOPES)
@pytest.mark.parametrize("supply_inbox", (True, False))
def test_disabled_direct_sleep_and_cold_app_deny_then_reenable(
    tmp_path: Path, scope: str, supply_inbox: bool,
) -> None:
    fixture = _fixture(tmp_path, scope)
    fixture.switch("run_maintenance", False)
    initial = fixture.stores.snapshot()
    fixture.run(supply_inbox=supply_inbox)
    report = _report(GhostSleepStore(tmp_path))
    assert report.cancelled and "hebbian_decay" in report.pending_steps
    assert fixture.stores.snapshot() == initial
    for _attempt in range(2):
        with closing(AppContext(tmp_path)) as app:
            assert not app.kick_ghost_sleep(project=fixture.project, session_id=fixture.session)
            assert app.wait_for_ghost_sleep(5)
            assert fixture.stores.snapshot() == initial
    fixture.switch("run_maintenance", True)
    with closing(AppContext(tmp_path)) as app:
        assert app.kick_ghost_sleep(project=fixture.project, session_id=fixture.session)
        assert app.wait_for_ghost_sleep(10)
        assert app.ghost_sleep_daemon.error_count == 0
    assert not _report(GhostSleepStore(tmp_path)).cancelled
    assert fixture.stores.snapshot() != initial
    assert [item.status for item in fixture.stores.queue.list_items()] == ["queued", "queued"]


@pytest.mark.parametrize("scope", SCOPES)
@pytest.mark.parametrize("control", ("accept_events", "retrieve_projections", "admit_hints"))
def test_other_controls_preserve_maintenance_of_accepted_state(tmp_path: Path, scope: str, control: str) -> None:
    fixture = _fixture(tmp_path, scope)
    fixture.switch(control, False)
    initial = fixture.stores.snapshot()
    fixture.run()
    report = _report(GhostSleepStore(tmp_path))
    assert not report.cancelled and not report.warnings
    assert all(step.ok for step in report.steps)
    assert fixture.stores.snapshot() != initial
    assert [item.status for item in fixture.stores.queue.list_items()] == ["queued", "queued"]
    controls = fixture.inbox.controls(project=fixture.project, session_id=fixture.session)
    assert not controls[control] and controls["run_maintenance"]


class UnreadableInbox(GhostInboxStore):
    def control_enabled(self, name: str, *, project: str = "", session_id: str = "") -> bool:
        if name == "run_maintenance":
            raise sqlite3.OperationalError("synthetic control read failure")
        return super().control_enabled(name, project=project, session_id=session_id)


@pytest.mark.parametrize("scope", SCOPES)
@pytest.mark.parametrize("fault", ("corrupt", "read_error", "foreign_authority"))
def test_bad_maintenance_authority_fails_closed_and_recovers(tmp_path: Path, scope: str, fault: str) -> None:
    fixture = _fixture(tmp_path / "home", scope)
    if fault == "corrupt":
        with fixture.inbox.records.transaction():
            fixture.inbox.records.set_setting("learning", {"learning_enabled": "corrupt"})
        authority = fixture.inbox
    elif fault == "read_error":
        authority = UnreadableInbox(fixture.home)
    else:
        authority = GhostInboxStore(tmp_path / "foreign")
    initial = fixture.stores.snapshot()
    sleep = GhostSleepStore(fixture.home)
    report = sleep.run_once(
        inbox_store=authority, hebbian_store=fixture.stores.hebbian, affinity_store=fixture.stores.affinity,
        continuity_store=fixture.stores.continuity, work_queue_store=fixture.stores.queue,
        project=fixture.project, session_id=fixture.session,
    )
    assert report.cancelled and report.steps[0].skipped_reason == "maintenance_control_failed"
    assert not report.steps[0].ok and report.steps[-1].ok
    assert any(warning.startswith("sleep_maintenance_control_failed:") for warning in report.warnings)
    assert fixture.stores.snapshot() == initial
    assert _report(GhostSleepStore(fixture.home)).cycle_id == report.cycle_id
    if fault == "corrupt":
        assert fixture.inbox.reset_all(preserve_settings=False)
    fixture.run()
    assert not _report(GhostSleepStore(fixture.home)).cancelled
    assert fixture.stores.snapshot() != initial


@pytest.mark.parametrize("scope", SCOPES)
@pytest.mark.parametrize("operation", ("delete_scope", "reset_preserve", "reset_settings"))
def test_explicit_control_lifecycle_is_still_available_while_maintenance_disabled(
    tmp_path: Path, scope: str, operation: str,
) -> None:
    fixture = _fixture(tmp_path, scope)
    fixture.switch("run_maintenance", False)
    if operation == "delete_scope":
        fixture.inbox.delete_scope(scope, project=fixture.project, session_id=fixture.session)
    else:
        assert fixture.inbox.reset_all(preserve_settings=operation == "reset_preserve")
    assert fixture.inbox.control_enabled(
        "run_maintenance", project=fixture.project, session_id=fixture.session,
    ) == (operation != "reset_preserve")
    initial = fixture.stores.snapshot()
    fixture.run(supply_inbox=False)
    report = _report(GhostSleepStore(tmp_path))
    assert report.cancelled == (operation == "reset_preserve")
    assert (fixture.stores.snapshot() == initial) == (operation == "reset_preserve")
    # Reconstruct from the actual same database, never from a cached allow.
    reopened = GhostInboxStore(tmp_path)
    assert reopened.controls(project=fixture.project, session_id=fixture.session) == fixture.inbox.controls(
        project=fixture.project, session_id=fixture.session,
    )


class FailingCompactionQueue(GhostWorkQueueStore):
    def compact_if_needed(self) -> dict[str, object]:
        super().compact_if_needed()
        raise sqlite3.OperationalError("synthetic failure after real queue compaction")


@pytest.mark.parametrize("scope", SCOPES)
def test_queue_reconciliation_rolls_back_if_same_admitted_compaction_fails(tmp_path: Path, scope: str) -> None:
    fixture = _fixture(tmp_path, scope)
    before_rows, before_events = fixture.stores.queue.records.rows(), fixture.stores.queue.records.events()
    report = GhostSleepStore(tmp_path).run_once(
        inbox_store=fixture.inbox, work_queue_store=FailingCompactionQueue(tmp_path),
        project=fixture.project, session_id=fixture.session,
    )
    step = next(step for step in report.steps if step.name == "event_compaction")
    assert not step.ok and step.counts["stores_failed"] == 1 and step.counts["stores_checked"] == 2
    assert "work_queue_compact_error:OperationalError" in step.warnings
    assert fixture.stores.queue.records.rows() == before_rows
    assert fixture.stores.queue.records.events() == before_events
    assert report.steps[-1].ok
    fixture.run()
    assert [item.status for item in fixture.stores.queue.list_items()] == ["queued", "queued"]


@pytest.mark.parametrize("scope", SCOPES)
def test_rebuilt_domains_do_not_restore_stale_maintenance_permission(tmp_path: Path, scope: str) -> None:
    fixture = _fixture(tmp_path, scope)
    fixture.switch("run_maintenance", False)
    assert fixture.stores.hebbian.rebuild_from_events()
    assert fixture.stores.affinity.rebuild_from_events()
    assert fixture.stores.continuity.rebuild_from_events()
    assert fixture.stores.queue.rebuild_from_events()
    before = fixture.stores.snapshot()
    fixture.run(supply_inbox=False)
    assert _report(GhostSleepStore(tmp_path)).cancelled
    assert fixture.stores.snapshot() == before


class FailingReportSleep(GhostSleepStore):
    def _write_state(self, report: GhostSleepReport) -> None:
        super()._write_state(report)
        raise sqlite3.OperationalError("synthetic failure after actual report write")


@pytest.mark.parametrize("enabled", (True, False))
def test_report_failure_preserves_previous_audit_and_cold_retry(tmp_path: Path, enabled: bool) -> None:
    sleep, inbox = GhostSleepStore(tmp_path), GhostInboxStore(tmp_path)
    previous = sleep.run_once()
    before_rows, before_events = sleep.records.rows(), sleep.records.events()
    assert inbox.set_control("run_maintenance", enabled)
    failed = FailingReportSleep(tmp_path).run_once()
    assert failed.cancelled != enabled
    assert not failed.steps[-1].ok and failed.steps[-1].skipped_reason == "report_write_failed"
    assert sleep.records.rows() == before_rows
    assert sleep.records.events() == before_events
    assert _report(GhostSleepStore(tmp_path)).cycle_id == previous.cycle_id
    resumed = GhostSleepStore(tmp_path).run_once()
    assert resumed.steps[-1].ok and resumed.cancelled != enabled
    assert _report(GhostSleepStore(tmp_path)).cycle_id == resumed.cycle_id


class PausedReconcileQueue(GhostWorkQueueStore):
    def __init__(self, home: Path, barrier: Barrier) -> None:
        super().__init__(home)
        self.barrier = barrier

    def reconcile_stale_claims(self, *, maintenance_as_of: str | None = None) -> GhostWorkSyncResult:
        self.barrier.pause()
        return super().reconcile_stale_claims(maintenance_as_of=maintenance_as_of)


@pytest.mark.parametrize("scope", SCOPES)
def test_control_read_and_queue_mutation_share_one_admission_transaction(tmp_path: Path, scope: str) -> None:
    fixture = _fixture(tmp_path, scope)
    barrier = Barrier()
    queue = PausedReconcileQueue(tmp_path, barrier)
    reports: list[bool] = []
    errors: list[str] = []
    committed = threading.Event()
    commit_snapshot: dict[str, object] = {}

    def work() -> None:
        try:
            report = GhostSleepStore(tmp_path).run_once(
                inbox_store=fixture.inbox, work_queue_store=queue,
                project=fixture.project, session_id=fixture.session,
            )
            reports.append(all(step.ok for step in report.steps))
        except Exception as exc:
            errors.append(f"worker {type(exc).__name__}: {exc}")

    def revoke() -> None:
        try:
            with GhostSQLite(tmp_path).write_transaction():
                fixture.switch("run_maintenance", False)
                commit_snapshot.update(fixture.stores.snapshot())
            committed.set()
        except Exception as exc:
            errors.append(f"writer {type(exc).__name__}: {exc}")

    worker = threading.Thread(target=work, daemon=True)
    writer = threading.Thread(target=revoke, daemon=True)
    try:
        worker.start()
        assert barrier.reached.wait(10)
        writer.start()
        assert not committed.wait(0.3), "deny committed between the authority read and admitted write"
    finally:
        barrier.release.set()
    worker.join(10)
    writer.join(10)
    assert not worker.is_alive() and not writer.is_alive()
    assert not errors and reports == [True], errors
    assert committed.is_set()
    assert fixture.stores.snapshot() == commit_snapshot
    assert [item.status for item in fixture.stores.queue.list_items()] == ["queued", "queued"]
    fixture.run()
    assert _report(GhostSleepStore(tmp_path)).cancelled
