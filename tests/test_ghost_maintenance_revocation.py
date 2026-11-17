"""Normal App Sleep must reread committed maintenance controls while running."""
from __future__ import annotations

import hashlib
import json
import threading
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from codey.app.context import AppContext
from codey.ghost.affinity import GhostAffinityStore
from codey.ghost.continuity import GhostContinuityStore
from codey.ghost.hebbian import GhostHebbianStore
from codey.ghost.inbox import GhostInboxStore
from codey.ghost.sleep import GhostSleepReport, GhostSleepStepResult, GhostSleepStore
from codey.ghost.sqlite import GhostSQLite
from codey.ghost.work_queue import GhostWorkQueueStore
from codey.storage.local_store import session_key
from tests.test_ghost_sleep_clock_boundary import _seed_hebbian, _seed_queue

SCOPES = ("user", "project", "session")
WINDOWS = ("before_worker", "after_health", "after_inbox_compaction")


@dataclass
class Barrier:
    reached: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)

    def pause(self) -> None:
        self.reached.set()
        if not self.release.wait(15):
            raise TimeoutError("maintenance barrier was not released")


class BoundarySleep(GhostSleepStore):
    def __init__(self, home: Path, barrier: Barrier) -> None:
        super().__init__(home)
        self.barrier = barrier

    def _projection_health_step(
        self, *, inbox_store: GhostInboxStore | None, hebbian_store: GhostHebbianStore | None,
        continuity_store: GhostContinuityStore | None, work_queue_store: GhostWorkQueueStore | None,
        affinity_store: GhostAffinityStore | None,
    ) -> GhostSleepStepResult:
        result = super()._projection_health_step(
            inbox_store=inbox_store, hebbian_store=hebbian_store, continuity_store=continuity_store,
            work_queue_store=work_queue_store, affinity_store=affinity_store,
        )
        assert result.ok, "real health must pass before the after-health boundary"
        self.barrier.pause()
        return result


class BoundaryInbox(GhostInboxStore):
    def __init__(self, home: Path, barrier: Barrier) -> None:
        super().__init__(home)
        self.barrier = barrier

    def compact_if_needed(self) -> dict[str, object]:
        result = super().compact_if_needed()
        assert result.get("ok") is True, "real inbox compaction must succeed"
        self.barrier.pause()
        return result


@dataclass(frozen=True)
class DomainStores:
    hebbian: GhostHebbianStore
    affinity: GhostAffinityStore
    continuity: GhostContinuityStore
    queue: GhostWorkQueueStore

    def snapshot(self) -> dict[str, object]:
        return {
            "hebbian_rows": self.hebbian.graph.rows(), "hebbian_events": self.hebbian.graph.events(),
            "affinity_rows": self.affinity.graph.rows(), "affinity_events": self.affinity.graph.events(),
            "continuity_rows": self.continuity.records.rows(), "continuity_events": self.continuity.records.events(),
            "queue_rows": self.queue.records.rows(), "queue_events": self.queue.records.events(),
        }


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _report(sleep: GhostSleepStore) -> GhostSleepReport:
    payload = sleep.records.payload()
    assert payload is not None, "normal App must save the actual Sleep report"
    report = GhostSleepReport.from_payload(payload.get("report"))
    assert report is not None
    return report


def _run_cell(home: Path, scope: str, window: str, revoked: bool) -> dict[str, object]:
    project = str((home / "project").resolve())
    session = "maintenance-revocation-session"
    scope_ref = project if scope == "project" else session if scope == "session" else ""
    barrier = Barrier()
    with closing(AppContext(home)) as app:
        stores = DomainStores(
            _seed_hebbian(home, scope, scope_ref), GhostAffinityStore(home), GhostContinuityStore(home),
            _seed_queue(home, scope, session_key(session) if scope == "session" else scope_ref, source_only=False),
        )
        app.ghost_hebbian, app.ghost_affinity = stores.hebbian, stores.affinity
        app.ghost_continuity, app.ghost_work_queue = stores.continuity, stores.queue
        if window == "after_health":
            app.ghost_sleep = BoundarySleep(home, barrier)
        if window == "after_inbox_compaction":
            app.ghost_inbox = BoundaryInbox(home, barrier)
        inbox, sleep = app.ghost_inbox, app.ghost_sleep
        assert inbox is not None and sleep is not None
        assert inbox.controls(project=project, session_id=session) == {
            "accept_events": True, "retrieve_projections": True, "run_maintenance": True, "admit_hints": True,
        }
        original_run = app.ghost_sleep_daemon.run_once

        def run(payload: dict[str, object]) -> None:
            if window == "before_worker":
                barrier.pause()
            original_run(payload)

        app.ghost_sleep_daemon.run_once = run
        initial = stores.snapshot()
        control_snapshot: dict[str, object] = {}
        writer_errors: list[str] = []
        committed = threading.Event()

        def write_control() -> None:
            try:
                # Capture the exact state preceding control commit, under its write lock.
                with GhostSQLite(home).write_transaction():
                    assert inbox.set_control(
                        "run_maintenance", not revoked, scope=scope, project=project, session_id=session,
                    )
                    assert inbox.control_enabled("run_maintenance", project=project, session_id=session) != revoked
                    control_snapshot.update(stores.snapshot())
                committed.set()
            except Exception as exc:
                writer_errors.append(f"{type(exc).__name__}: {exc}")

        writer = threading.Thread(target=write_control, name="maintenance-control-writer", daemon=True)
        original_stop = app.ghost_sleep_daemon.stop_requested

        def stop_requested() -> bool:
            # After a held store transaction releases, give the waiting writer
            # its turn before the next real admission. Return the actual stop state.
            if barrier.release.is_set() and barrier.reached.is_set() and writer.ident is not None:
                assert committed.wait(15), "control writer did not commit before the next admission"
            return original_stop()

        app.ghost_sleep_daemon.stop_requested = stop_requested
        try:
            assert app.kick_ghost_sleep(project=project, session_id=session)
            assert barrier.reached.wait(15), "real worker did not reach the requested boundary"
            writer.start()
            committed_while_paused = committed.wait(0.3)
        finally:
            barrier.release.set()
        writer.join(15)
        assert not writer.is_alive() and not writer_errors, writer_errors
        assert committed.is_set(), "control commit was not observed"
        assert app.wait_for_ghost_sleep(15), "Sleep failed to finish"
        assert app.ghost_sleep_daemon.error_count == 0, app.ghost_sleep_daemon.last_error
        final = stores.snapshot()
        report = _report(sleep)
        assert all(step.ok for step in report.steps), report.to_payload()
        assert not report.warnings, report.warnings
        queue = stores.queue.list_items(scope=scope, project=project, session_id=session)
        assert len(queue) == 2
        changed = sorted(key for key in final if final[key] != control_snapshot[key])
        measurement: dict[str, object] = {
            "scope": scope, "window": window, "revoked": revoked, "fixture_valid": True,
            "committed_while_paused": committed_while_paused,
            "initial_digest": _digest(initial), "at_control_digest": _digest(control_snapshot),
            "final_digest": _digest(final), "post_commit_changes": changed,
            "cancelled": report.cancelled, "pending_steps": list(report.pending_steps),
            "queue_statuses": [item.status for item in queue],
            "pass": (not changed and report.cancelled) if revoked else (
                final != initial and not report.cancelled and all(item.status == "queued" for item in queue)
            ),
        }
        print("MAINTENANCE_REVOCATION_CELL " + json.dumps(measurement, sort_keys=True), flush=True)
        return measurement


@pytest.mark.parametrize("scope", SCOPES)
@pytest.mark.parametrize("window", WINDOWS)
@pytest.mark.parametrize("revoked", (False, True))
def test_normal_app_sleep_obeys_committed_maintenance_control(
    tmp_path: Path, scope: str, window: str, revoked: bool,
) -> None:
    result = _run_cell(tmp_path, scope, window, revoked)
    assert result["pass"], result
