"""Different Ghost deletion operations interleaved with real runtime writers."""

from __future__ import annotations

import argparse
import json
from collections.abc import Generator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from typing import Literal
from unittest.mock import patch

from codey.app.context import AppContext
from codey.ghost import _common
from codey.ghost.control_surface import GhostControlSurface
from codey.ghost.deletion_manifest import DeletionManifest, new_manifest
from codey.ghost.deletion_manifest_coordinator import (
    GHOST_STORE_NAMES,
    DeletionManifestCoordinator,
    DeletionTarget,
)
from codey.ghost.deletion_manifest_store import DeletionManifestStore
from codey.ghost.schema import GhostSignal
from codey.ghost.sqlite import GhostSQLite
from tests.manual.ghost_memory_routing.deletion_runtime_probe import _targets
from tests.manual.ghost_memory_routing.projection_delete_probe import _target_records, has_projection
from tests.test_ghost_received_input_lifetime import _receive, _run

Scenario = Literal["same_project", "other_project", "session", "reset"]
SCENARIOS: tuple[Scenario, ...] = ("same_project", "other_project", "session", "reset")
WRITERS = ("continuity", "work_queue", "affinity", "sleep")
RUNS = ("old-run", "other-run", "control-run")
SESSION = "received-session"


def _wait(event: Event) -> None:
    if not event.wait(25):
        raise TimeoutError("mixed-lifecycle experiment did not reach its frozen boundary")


class PausedAfterInbox(DeletionManifestStore):
    def __init__(self, database: GhostSQLite) -> None:
        super().__init__(database)
        self.ready, self.release = Event(), Event()
        self.armed = False

    def append(self, event: Mapping[str, object]) -> DeletionManifest:
        updated = super().append(event)
        if event.get("store") == "inbox" and event.get("status") == "completed":
            self.armed = True
        return updated

    @contextmanager
    def transaction(self) -> Generator[None]:
        with super().transaction():
            yield
        if self.armed and not self.ready.is_set() and self._connection.get() is None:
            self.ready.set()
            _wait(self.release)


@dataclass
class Fault:
    enabled: bool
    injected: bool = False


@dataclass(frozen=True)
class Cell:
    scenario: Scenario
    writer: str
    fault: bool
    writer_first: bool
    valid: bool
    writer_ok: bool
    a_status: str
    b_status: str
    recovery_status: str
    observation_counts: tuple[int, ...]
    old_project_rows: int
    fresh_observations: int
    fresh_continuity: int
    replay_ok: bool
    duplicate_calls: int
    a_stores: tuple[str, ...]
    b_stores: tuple[str, ...]
    recovery_stores: tuple[str, ...]
    sleep_ok: bool
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def passed(self) -> bool:
        expected_other = int(self.scenario == "same_project")
        expected_control = int(self.scenario in {"same_project", "other_project"})
        return (
            self.valid and self.writer_ok == self.writer_first
            and self.a_status == ("blocked" if self.fault else "completed")
            and self.b_status == "completed" and self.recovery_status == "completed"
            and self.observation_counts == (0, expected_other, expected_control)
            and self.old_project_rows == 0 and self.fresh_observations == 1
            and self.fresh_continuity > 0 and self.replay_ok and self.duplicate_calls == 0 and self.sleep_ok
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "scenario": self.scenario, "writer": self.writer, "fault": self.fault,
            "writer_first": self.writer_first, "valid": self.valid, "passed": self.passed,
            "writer_ok": self.writer_ok, "a_status": self.a_status, "b_status": self.b_status,
            "recovery_status": self.recovery_status, "old_observation_counts": self.observation_counts,
            "old_project_rows": self.old_project_rows, "fresh_observations": self.fresh_observations,
            "fresh_continuity": self.fresh_continuity, "replay_ok": self.replay_ok,
            "duplicate_calls": self.duplicate_calls, "warnings": self.warnings,
            "a_stores": self.a_stores, "b_stores": self.b_stores,
            "recovery_stores": self.recovery_stores, "sleep_ok": self.sleep_ok,
        }


def _manifest(identity: str, project: str, *, scenario: Scenario = "same_project") -> DeletionManifest:
    return new_manifest(
        operation_id=identity, action="reset_all" if scenario == "reset" else "delete_scope",
        scope="all" if scenario == "reset" else "session" if scenario == "session" else "project",
        project=project, session_id=SESSION, requested_at="2026-11-17T00:00:00Z",
        retention_class="tombstone", stores=GHOST_STORE_NAMES,
    )


def _seed_preference(surface: GhostControlSurface, project: str, run_id: str) -> None:
    assert surface.inbox is not None and surface.hebbian is not None
    created = surface.inbox.ingest_signals((GhostSignal(
        kind="style_preference", scope="project", summary="Concise answers.",
        evidence_quote="What needs verification?", confidence=1.0, source="manual",
        metadata={"conflict_key": "reply_length", "value_key": "concise"},
    ),), session_id=SESSION, run_id=run_id, project=project, user_text="What needs verification?")
    assert len(created) == 1
    accepted = surface.inbox.review_candidate(created[0].id, "accept", reviewed_by="mixed-lifecycle-fixture")
    assert accepted is not None and surface.hebbian.reinforce_candidate(accepted).applied


def _write(surface: GhostControlSurface, writer: str, project: str) -> bool:
    assert surface.continuity is not None and surface.work_queue is not None
    assert surface.affinity is not None and surface.sleep is not None
    if writer == "continuity":
        return surface.continuity.sync_from_sources(
            hebbian_store=surface.hebbian, user_focus_excerpt="What needs verification?",
            run_id="old-run", session_id=SESSION, project=project, mode="chat",
        ).ok
    if writer == "work_queue":
        return surface.work_queue.sync_from_sources(
            continuity_store=surface.continuity, session_id=SESSION, project=project,
        ).ok
    if writer == "affinity":
        return surface.affinity.sync_from_sources(
            hebbian_store=surface.hebbian, work_queue_store=surface.work_queue,
            session_id=SESSION, project=project,
        ).ok
    return surface.sleep.run_once(
        inbox_store=surface.inbox, hebbian_store=surface.hebbian, continuity_store=surface.continuity,
        work_queue_store=surface.work_queue, affinity_store=surface.affinity,
        run_id="old-run", session_id=SESSION, project=project,
    ).steps[-1].ok


def _observation_count(surface: GhostControlSurface, run_id: str) -> int:
    assert surface.observations is not None
    return sum(row.get("run_id") == run_id for row in surface.observations.read_all())


def _old_project_rows(surface: GhostControlSurface, project: str) -> int:
    assert surface.inbox is not None and surface.hebbian is not None
    assert surface.continuity is not None and surface.work_queue is not None
    return sum((
        sum(candidate.run_id == "old-run" and candidate.project == project for candidate in surface.inbox.list_candidates()),
        sum(node.scope_ref == project and node.status == "active" for node in surface.hebbian.list_nodes()),
        sum(item.source_ref == "old-run" and item.scope_ref == project for item in surface.continuity.list_items()),
        sum(item.source_ref == "old-run" and item.scope_ref == project for item in surface.work_queue.list_items()),
    ))


def _drain(coordinator: DeletionManifestCoordinator, manifest: DeletionManifest) -> DeletionManifest:
    current = coordinator.run(manifest)
    while current.pending_stores:
        previous = len(current.pending_stores)
        current = coordinator.run(manifest)
        if len(current.pending_stores) >= previous:
            raise RuntimeError("manifest resume made no progress")
    return current


def _statuses(manifest: DeletionManifest) -> tuple[str, ...]:
    return tuple(f"{item.store}:{item.status}:{item.detail}" for item in manifest.stores)


def run_cell(home: Path, scenario: Scenario, writer: str, fault: bool, *, writer_first: bool = False) -> Cell:
    home = home.resolve()
    with closing(AppContext(home, start_scheduler=False)) as state:
        projects = tuple(str(home / name) for name in ("project-a", "project-b", "project-c"))
        surface = GhostControlSurface.from_state_home(home)
        assert surface.observations is not None
        for run_id, project in zip(RUNS, projects, strict=True):
            _run(state, _receive(state, project, run_id=run_id))
            _seed_preference(surface, project, run_id)
        for name in WRITERS:
            assert _write(surface, name, projects[0])
        seeded = all(has_projection(surface, name, project=projects[0]) for name in WRITERS)
        scope_identity_ok = all(project == _common.normalize_project(project) for project in projects)
        seed_project_rows = _old_project_rows(surface, projects[0])
        old_rows = tuple(_observation_count(surface, run_id) for run_id in RUNS)
        old_owner = GhostControlSurface.from_state_home(home)
        records = _target_records(old_owner, writer)
        original_transaction = records.transaction
        ready, release = Event(), Event()

        @contextmanager
        def before_write(*, write: bool = True, deleting: bool = False) -> Generator[None]:
            if write and not ready.is_set():
                ready.set()
                _wait(release)
            with original_transaction(write=write, deleting=deleting):
                yield

        paused = PausedAfterInbox(surface.observations.database)
        targets = _targets(surface)
        continuity_target = targets["continuity"]
        injection = Fault(fault)

        def delete_continuity(scope: str, *, project: str = "", session_id: str = "") -> object:
            result = continuity_target.delete_scope(scope, project=project, session_id=session_id)
            if injection.enabled and not injection.injected:
                injection.injected = True
                raise OSError("mixed-lifecycle continuity mutation failure")
            return result

        targets["continuity"] = DeletionTarget(delete_continuity, continuity_target.reset_all)
        first = DeletionManifestCoordinator(paused, targets)
        second_surface = GhostControlSurface.from_state_home(home)
        assert second_surface.observations is not None
        second = DeletionManifestCoordinator(DeletionManifestStore(second_surface.observations.database), _targets(second_surface))
        a_manifest = _manifest("mixed-a", projects[0])
        b_manifest = _manifest("mixed-b", projects[1] if scenario == "other_project" else projects[0], scenario=scenario)
        with patch.object(records, "transaction", before_write), ThreadPoolExecutor(max_workers=2) as executor:
            pending_writer = executor.submit(_write, old_owner, writer, projects[0])
            try:
                _wait(ready)
                if writer_first:
                    release.set()
                    writer_ok = pending_writer.result(timeout=30)
                pending_first = executor.submit(first.run, a_manifest)
                _wait(paused.ready)
                # This independent coordinator runs while A is paused after
                # inbox commit; no test waits while holding a SQLite write lock.
                b_result = _drain(second, b_manifest)
                paused.release.set()
                a_result = pending_first.result(timeout=30)
                if a_result.pending_stores:
                    a_result = _drain(first, a_manifest)
                recovery_status = a_result.status
                recovery_result = a_result
                if fault:
                    recovery_result = _drain(second, _manifest("mixed-recovery", projects[0]))
                    recovery_status = recovery_result.status
                _run(state, _receive(state, projects[0], run_id="fresh-run"))
                fresh_learned = _observation_count(surface, "fresh-run") == 1
                release.set()
                if not writer_first:
                    writer_ok = pending_writer.result(timeout=30)
            finally:
                release.set()
                paused.release.set()
        reopened = GhostControlSurface.from_state_home(home)
        assert reopened.hebbian is not None and reopened.continuity is not None
        assert reopened.work_queue is not None and reopened.affinity is not None and reopened.sleep is not None
        replay_ok = all((
            reopened.hebbian.rebuild_from_events(), reopened.continuity.rebuild_from_events(),
            reopened.work_queue.rebuild_from_events(), reopened.affinity.rebuild_from_events(),
        ))
        report = reopened.sleep.run_once(
            inbox_store=reopened.inbox, hebbian_store=reopened.hebbian, continuity_store=reopened.continuity,
            work_queue_store=reopened.work_queue, affinity_store=reopened.affinity,
            run_id="fresh-run", session_id=SESSION, project=projects[0],
        )
        calls: list[str] = []

        def forbidden_delete(scope: str, *, project: str = "", session_id: str = "") -> object:
            calls.append(scope)
            raise AssertionError("completed operation called a store again")

        def forbidden_reset() -> object:
            calls.append("reset")
            raise AssertionError("completed operation called reset again")

        forbidden = {name: DeletionTarget(forbidden_delete, forbidden_reset) for name in GHOST_STORE_NAMES}
        retry = DeletionManifestCoordinator(second.manifests, forbidden)
        a_replay = retry.run(a_manifest)
        b_replay = retry.run(b_manifest)
        replay_ok = replay_ok and a_replay.status == a_result.status and b_replay.status == b_result.status
        final_owner = GhostControlSurface.from_state_home(home)
        assert final_owner.continuity is not None
        return Cell(
            scenario, writer, fault, writer_first,
            seeded and scope_identity_ok and seed_project_rows > 0
            and old_rows == (1, 1, 1) and fresh_learned and paused.ready.is_set()
            and injection.injected == fault,
            writer_ok, a_result.status, b_result.status, recovery_status,
            tuple(_observation_count(final_owner, run_id) for run_id in RUNS),
            _old_project_rows(final_owner, projects[0]), _observation_count(final_owner, "fresh-run"),
            sum(item.source_ref == "fresh-run" and item.scope_ref == projects[0] for item in final_owner.continuity.list_items()),
            replay_ok, len(calls), _statuses(a_result), _statuses(b_result), _statuses(recovery_result),
            report.steps[-1].ok, report.warnings,
        )


def _save(output: Path, cells: list[Cell], *, complete: bool) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1, "baseline": "28aae81c", "model": None,
        "complete": complete, "expected_cells": 36,
        "cell_count": len(cells), "valid": sum(cell.valid for cell in cells),
        "passed": sum(cell.passed for cell in cells), "cells": [cell.to_payload() for cell in cells],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    output: object = arguments.output
    if not isinstance(output, Path):
        raise TypeError("output must be a path")
    cells: list[Cell] = []
    _save(output, cells, complete=False)
    with TemporaryDirectory(prefix="codey-manifest-mixed-") as temporary:
        for scenario in SCENARIOS:
            for writer in WRITERS:
                for fault in (False, True):
                    print(f"cell={len(cells)} scenario={scenario} writer={writer} fault={fault}", flush=True)
                    cells.append(run_cell(Path(temporary) / str(len(cells)), scenario, writer, fault))
                    _save(output, cells, complete=False)
        for writer in WRITERS:
            print(f"cell={len(cells)} scenario=same_project writer={writer} writer_first=True", flush=True)
            cells.append(run_cell(Path(temporary) / str(len(cells)), "same_project", writer, False, writer_first=True))
            _save(output, cells, complete=False)
    payload = _save(output, cells, complete=True)
    summary: dict[str, object] = {key: value for key, value in payload.items() if key != "cells"}
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
