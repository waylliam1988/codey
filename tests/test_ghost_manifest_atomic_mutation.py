"""Real Ghost mutations and their manifest acknowledgement share a commit."""

from __future__ import annotations

import multiprocessing
import os
import sqlite3
from collections.abc import Generator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from multiprocessing.synchronize import Event as ProcessEvent
from pathlib import Path
from threading import Event

import pytest

from codey.app.context import AppContext
from codey.ghost.control_surface import GhostControlSurface
from codey.ghost.deletion_manifest import DeletionManifest, new_manifest
from codey.ghost.deletion_manifest_coordinator import (
    GHOST_STORE_NAMES,
    DeletionManifestCoordinator,
    DeletionTarget,
)
from codey.ghost.deletion_manifest_store import DeletionManifestStore
from codey.ghost.sqlite import GhostSQLite
from tests.manual.ghost_memory_routing.deletion_runtime_probe import _targets
from tests.test_ghost_deletion_manifest_recovery import _seed_all_stores
from tests.test_ghost_received_input_lifetime import _receive, _run

CASES: tuple[tuple[str, str], ...] = (
    ("delete_scope", "user"), ("delete_scope", "project"),
    ("delete_scope", "session"), ("reset_all", "all"),
)
TABLES = (
    "memory_events", "memory_records", "memory_consumers", "ghost_settings",
    "graph_events", "graph_nodes", "graph_edges", "graph_consumers",
    "observation_events", "memory_embeddings", "ghost_input_origins",
    "ghost_retired_inputs", "ghost_retired_sources", "ghost_projection_epoch",
)


def _manifest(action: str, scope: str, project: str, *, stores: tuple[str, ...] = GHOST_STORE_NAMES) -> DeletionManifest:
    return new_manifest(
        operation_id="atomic-operation", action=action, scope=scope, project=project,
        session_id="received-session", requested_at="2026-11-17T00:00:00Z",
        retention_class="tombstone", stores=stores,
    )


def _live_count(home: Path, run_id: str) -> int:
    return _live_count_database(GhostSQLite(home), run_id)


def _live_count_database(owner: GhostSQLite, run_id: str) -> int:
    with owner.connection() as database:
        row: sqlite3.Row | None = database.execute(
            "SELECT count(*) FROM observation_events WHERE run_id=? "
            "AND NOT EXISTS (SELECT 1 FROM ghost_retired_inputs "
            "WHERE kind='observation' AND identity=observation_events.run_id)", (run_id,),
        ).fetchone()
    assert row is not None
    count: object = row[0]
    assert type(count) is int
    return count


def _row_values(row: sqlite3.Row) -> tuple[object, ...]:
    values: list[object] = []
    for index in range(len(row)):
        value: object = row[index]
        values.append(value)
    return tuple(values)


def _data_state(database: GhostSQLite) -> dict[str, tuple[tuple[object, ...], ...]]:
    state: dict[str, tuple[tuple[object, ...], ...]] = {}
    with database.connection() as connection:
        for table in TABLES:
            rows: list[sqlite3.Row] = connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
            state[table] = tuple(_row_values(row) for row in rows)
    return state


class PausedStart(DeletionManifestStore):
    def __init__(self, database: GhostSQLite) -> None:
        super().__init__(database)
        self.ready, self.release, self.attempted = Event(), Event(), Event()
        self.resuming = False

    def start(self, manifest: DeletionManifest) -> DeletionManifest:
        snapshot = super().start(manifest)
        self.ready.set()
        assert self.release.wait(20)
        self.resuming = True
        return snapshot

    @contextmanager
    def transaction(self) -> Generator[None]:
        if self.resuming:
            self.attempted.set()
        with super().transaction():
            yield


@pytest.mark.parametrize("action,scope", CASES)
@pytest.mark.parametrize("release_at", ["before_ack", "after_completion"])
def test_stale_pending_coordinator_cannot_repeat_mutation(
    tmp_path: Path, action: str, scope: str, release_at: str,
) -> None:
    with closing(AppContext(tmp_path, start_scheduler=False)) as state:
        project = str(tmp_path / "project")
        _run(state, _receive(state, project, run_id="old-run"))
        first_surface = GhostControlSurface.from_state_home(tmp_path)
        second_surface = GhostControlSurface.from_state_home(tmp_path)
        assert first_surface.observations is not None and second_surface.observations is not None
        targets = _targets(first_surface)
        original = targets["observations"]
        mutated, acknowledge = Event(), Event()
        first_calls: list[object] = []
        second_calls: list[object] = []

        def first_delete(scope: str = "", *, project: str = "", session_id: str = "") -> object:
            result = (
                original.delete_scope(scope, project=project, session_id=session_id)
                if scope else original.reset_all()
            )
            first_calls.append(result)
            mutated.set()
            assert acknowledge.wait(20)
            return result

        def first_reset() -> object:
            return first_delete()

        targets["observations"] = DeletionTarget(first_delete, first_reset)
        first = DeletionManifestCoordinator(DeletionManifestStore(first_surface.observations.database), targets)
        second_targets = _targets(second_surface)
        other = second_targets["observations"]

        def second_delete(scope: str = "", *, project: str = "", session_id: str = "") -> object:
            second_calls.append(scope)
            return other.delete_scope(scope, project=project, session_id=session_id) if scope else other.reset_all()

        def second_reset() -> object:
            return second_delete()

        second_targets["observations"] = DeletionTarget(second_delete, second_reset)
        paused = PausedStart(second_surface.observations.database)
        second = DeletionManifestCoordinator(paused, second_targets)
        manifest = _manifest(action, scope, project)
        with ThreadPoolExecutor(max_workers=2) as executor:
            try:
                pending_second = executor.submit(second.run, manifest)
                assert paused.ready.wait(20)
                pending_first = executor.submit(first.run, manifest)
                assert mutated.wait(20)
                # Actual deletion already ran inside the first transaction.
                # Independent readers still see the previous committed row.
                assert _live_count_database(first_surface.observations.database, "old-run") == 1
                if release_at == "before_ack":
                    paused.release.set()
                    assert paused.attempted.wait(20)
                    assert not second_calls
                acknowledge.set()
                assert pending_first.result(timeout=25).status == "completed"
                assert _live_count(tmp_path, "old-run") == 0
                _run(state, _receive(state, project, run_id="fresh-run"))
                assert _live_count(tmp_path, "fresh-run") == 1
                paused.release.set()
                assert pending_second.result(timeout=25).status == "completed"
            finally:
                acknowledge.set()
                paused.release.set()
        assert len(first_calls) == 1 and not second_calls
        assert _live_count(tmp_path, "fresh-run") == 1
        assert len(first.manifests.events(manifest.operation_id)) == 8


@pytest.mark.parametrize("name", GHOST_STORE_NAMES)
@pytest.mark.parametrize("action,scope", [("delete_scope", "project"), ("reset_all", "all")])
@pytest.mark.parametrize("fault", ["raise", "false", "ack"])
def test_each_store_mutation_rolls_back_with_failed_acknowledgement(
    tmp_path: Path, name: str, action: str, scope: str, fault: str,
) -> None:
    surface = _seed_all_stores(tmp_path)
    assert surface.observations is not None
    database = surface.observations.database
    targets = _targets(surface)
    original = targets[name]
    called: list[object] = []

    def delete(scope: str = "", *, project: str = "", session_id: str = "") -> object:
        result = (
            original.delete_scope(scope, project=project, session_id=session_id)
            if scope else original.reset_all()
        )
        called.append(result)
        if fault == "raise":
            raise OSError("after actual store mutation")
        return False if fault == "false" else result

    def reset() -> object:
        return delete()

    targets[name] = DeletionTarget(delete, reset)
    manifests = DeletionManifestStore(database)
    manifest = _manifest(action, scope, str(tmp_path / "project-delete"), stores=(name,))
    manifests.start(manifest)
    if fault == "ack":
        with database.connection() as connection:
            connection.execute(
                "CREATE TRIGGER reject_manifest_completed BEFORE INSERT ON ghost_deletion_manifest_events "
                "WHEN json_extract(NEW.payload,'$.status')='completed' "
                "BEGIN SELECT RAISE(ABORT,'injected acknowledgement failure'); END",
            )
    before = _data_state(database)
    result = DeletionManifestCoordinator(manifests, targets).run(manifest)
    assert called and result.status == "blocked" and result.store(name).status == "blocked"
    assert len(manifests.events(manifest.operation_id)) == 2
    assert _data_state(GhostSQLite(tmp_path)) == before


class CrashManifestStore(DeletionManifestStore):
    def __init__(self, database: GhostSQLite, stage: str) -> None:
        super().__init__(database)
        self.stage = stage
        self.armed = False

    def append(self, event: Mapping[str, object]) -> DeletionManifest:
        result = super().append(event)
        if event.get("store") == "observations" and event.get("status") == "completed":
            self.armed = True
            if self.stage == "ack":
                os._exit(23)
        return result

    @contextmanager
    def transaction(self) -> Generator[None]:
        with super().transaction():
            yield
        if self.armed and self.stage == "commit" and self._connection.get() is None:
            os._exit(23)


def _crash_child(home: str, action: str, scope: str, stage: str) -> None:
    surface = GhostControlSurface.from_state_home(home)
    assert surface.observations is not None
    targets = _targets(surface)
    original = targets["observations"]

    def delete(scope: str = "", *, project: str = "", session_id: str = "") -> object:
        result = (
            original.delete_scope(scope, project=project, session_id=session_id)
            if scope else original.reset_all()
        )
        if stage == "mutation":
            os._exit(23)
        return result

    def reset() -> object:
        return delete()

    targets["observations"] = DeletionTarget(delete, reset)
    coordinator = DeletionManifestCoordinator(CrashManifestStore(surface.observations.database, stage), targets)
    coordinator.run(_manifest(action, scope, str(Path(home) / "project")))
    raise AssertionError("child did not reach the requested hard-exit boundary")


@pytest.mark.parametrize("action,scope", CASES)
@pytest.mark.parametrize("stage", ["mutation", "ack", "commit"])
def test_process_death_cannot_separate_delete_and_ack(
    tmp_path: Path, action: str, scope: str, stage: str,
) -> None:
    project = str(tmp_path / "project")
    with closing(AppContext(tmp_path, start_scheduler=False)) as state:
        _run(state, _receive(state, project, run_id="old-run"))
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_child, args=(str(tmp_path), action, scope, stage),
    )
    process.start()
    process.join(25)
    if process.is_alive():
        process.terminate()
        process.join(5)
        pytest.fail("hard-exit child timed out")
    assert process.exitcode == 23
    surface = GhostControlSurface.from_state_home(tmp_path)
    assert surface.observations is not None
    coordinator = DeletionManifestCoordinator(DeletionManifestStore(surface.observations.database), _targets(surface))
    manifest = _manifest(action, scope, project)
    recovered = coordinator.manifests.get(manifest.operation_id)
    assert recovered.store("observations").status == ("completed" if stage == "commit" else "pending")
    assert _live_count(tmp_path, "old-run") == (0 if stage == "commit" else 1)
    assert coordinator.run(manifest).status == "completed"
    assert _live_count(tmp_path, "old-run") == 0
    with closing(AppContext(tmp_path, start_scheduler=False)) as state:
        _run(state, _receive(state, project, run_id="fresh-run"))
        assert _live_count(tmp_path, "fresh-run") == 1
        assert coordinator.run(manifest).status == "completed"
        assert _live_count(tmp_path, "fresh-run") == 1


class ProcessPausedStart(DeletionManifestStore):
    def __init__(self, database: GhostSQLite, ready: ProcessEvent, release: ProcessEvent) -> None:
        super().__init__(database)
        self.ready = ready
        self.release = release

    def start(self, manifest: DeletionManifest) -> DeletionManifest:
        snapshot = super().start(manifest)
        self.ready.set()
        assert self.release.wait(30)
        return snapshot


def _resume_child(
    home: str, action: str, scope: str, ready: ProcessEvent, release: ProcessEvent, report: str,
) -> None:
    surface = GhostControlSurface.from_state_home(home)
    assert surface.observations is not None
    targets = _targets(surface)
    original = targets["observations"]
    calls: list[object] = []

    def delete(scope: str = "", *, project: str = "", session_id: str = "") -> object:
        result = (
            original.delete_scope(scope, project=project, session_id=session_id)
            if scope else original.reset_all()
        )
        calls.append(result)
        return result

    def reset() -> object:
        return delete()

    targets["observations"] = DeletionTarget(delete, reset)
    coordinator = DeletionManifestCoordinator(
        ProcessPausedStart(surface.observations.database, ready, release), targets,
    )
    result = coordinator.run(_manifest(action, scope, str(Path(home) / "project")))
    Path(report).write_text(f"{result.status}\n{len(calls)}\n", encoding="utf-8")


@pytest.mark.parametrize("action,scope", CASES)
def test_two_processes_resume_same_pending_operation_once(tmp_path: Path, action: str, scope: str) -> None:
    project = str(tmp_path / "project")
    with closing(AppContext(tmp_path, start_scheduler=False)) as state:
        _run(state, _receive(state, project, run_id="old-run"))
    context = multiprocessing.get_context("spawn")
    first_ready, second_ready = context.Event(), context.Event()
    first_release, second_release = context.Event(), context.Event()
    first_report, second_report = tmp_path / "first.txt", tmp_path / "second.txt"
    first = context.Process(
        target=_resume_child,
        args=(str(tmp_path), action, scope, first_ready, first_release, str(first_report)),
    )
    second = context.Process(
        target=_resume_child,
        args=(str(tmp_path), action, scope, second_ready, second_release, str(second_report)),
    )
    first.start()
    second.start()
    try:
        assert first_ready.wait(20) and second_ready.wait(20)
        first_release.set()
        first.join(25)
        assert first.exitcode == 0
        assert first_report.read_text(encoding="utf-8").splitlines() == ["completed", "1"]
        assert _live_count(tmp_path, "old-run") == 0
        with closing(AppContext(tmp_path, start_scheduler=False)) as state:
            _run(state, _receive(state, project, run_id="fresh-run"))
            assert _live_count(tmp_path, "fresh-run") == 1
            second_release.set()
            second.join(25)
            assert second.exitcode == 0
            assert second_report.read_text(encoding="utf-8").splitlines() == ["completed", "0"]
            assert _live_count(tmp_path, "fresh-run") == 1
    finally:
        first_release.set()
        second_release.set()
        for process in (first, second):
            process.join(5)
            if process.is_alive():
                process.terminate()
                process.join(5)


def test_nested_write_failure_rolls_back_only_its_savepoint(tmp_path: Path) -> None:
    database = GhostSQLite(tmp_path)
    other_owner = GhostSQLite(tmp_path)
    with database.write_transaction() as first:
        first.execute("INSERT INTO ghost_settings(key,value) VALUES('outer','1')")
        with pytest.raises(OSError, match="nested failure"), other_owner.write_transaction() as nested:
            nested.execute("INSERT INTO ghost_settings(key,value) VALUES('nested','2')")
            raise OSError("nested failure")
        # Creating an owner in the transaction must not initialize schema via
        # executescript, which would implicitly commit its parent's writes.
        constructed = GhostSQLite(tmp_path)
        with constructed.connection() as joined:
            assert joined is first
            row: sqlite3.Row | None = joined.execute("SELECT 1 FROM ghost_settings WHERE key='nested'").fetchone()
            assert row is None
        first.execute("INSERT INTO ghost_settings(key,value) VALUES('after','3')")
    with other_owner.connection() as committed:
        rows: list[sqlite3.Row] = committed.execute("SELECT key FROM ghost_settings ORDER BY key").fetchall()
        assert tuple(_row_values(row) for row in rows) == (("after",), ("outer",))


def test_different_ghost_paths_never_join_a_transaction(tmp_path: Path) -> None:
    first = GhostSQLite(tmp_path / "first")
    second = GhostSQLite(tmp_path / "second")
    with pytest.raises(OSError, match="outer failure"), first.write_transaction() as connection:
        connection.execute("INSERT INTO ghost_settings(key,value) VALUES('first','1')")
        with second.write_transaction() as independent:
            assert independent is not connection
            independent.execute("INSERT INTO ghost_settings(key,value) VALUES('second','2')")
        raise OSError("outer failure")
    with first.connection() as failed:
        failed_row: sqlite3.Row | None = failed.execute("SELECT 1 FROM ghost_settings").fetchone()
        assert failed_row is None
    with second.connection() as succeeded:
        row: sqlite3.Row | None = succeeded.execute("SELECT key FROM ghost_settings").fetchone()
        assert row is not None and _row_values(row) == ("second",)
