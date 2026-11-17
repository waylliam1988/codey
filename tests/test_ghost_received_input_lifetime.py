"""Ghost deletion includes canonical inputs that have not begun execution."""

from __future__ import annotations

import multiprocessing
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Event
from typing import Protocol, runtime_checkable

import pytest

from codey.app.context import AppContext
from codey.app.durable_services import DurableAppServices
from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.app.task_services import build_task_deps
from codey.durable import DurableSession, SQLiteDurableStore
from codey.durable.model import CommitResult, DurableMutation, TaskCheckpoint
from codey.ghost.control_surface import GhostControlSurface
from codey.ghost.observations import GhostObservationStore
from codey.ghost.payload_types import mapping_fields
from codey.ghost.sqlite import GhostSQLite
from codey.operations.task_entry import run_task_submission
from codey.operations.task_run import TaskRunDeps
from codey.providers.base import ChatProvider
from codey.task.model import TaskSubmission
from tests.manual.ghost_memory_routing.inflight_delete_probe import DelayedProvider
from tests.manual.ghost_memory_routing.received_input_delete_probe import received_cell


@pytest.mark.parametrize("action,scope", [
    ("delete_scope", "user"), ("delete_scope", "project"),
    ("delete_scope", "session"), ("reset_all", "user"),
])
@pytest.mark.parametrize("enabled", [True, False])
def test_received_input_is_not_relearned_after_deletion(
    tmp_path: Path, action: str, scope: str, enabled: bool,
) -> None:
    result = received_cell(tmp_path, action, scope, enabled)
    assert result.admitted_before_entry and result.delete_ok and result.task_completed
    assert result.observation_rows == 0
    assert result.continuity_rows == 0


@runtime_checkable
class InputQueuePort(Protocol):
    def create(self, identity: str, request: dict[str, object]) -> object: ...

    def edit(self, identity: str, revision: int, text: str) -> object: ...


def _queue(state: AppContext) -> InputQueuePort:
    queue: object = state.input_queue
    assert isinstance(queue, InputQueuePort)
    return queue


def _receive(state: AppContext, project: str, *, run_id: str = "received-run") -> TaskSubmission:
    request = TaskSubmission(
        "received-session", project, "What needs verification?", 2, False, "deepseek",
        intent="chat", run_id=run_id,
    )
    _queue(state).create(run_id, {
        "session_id": request.session_id, "project": project, "run_id": run_id,
        "task": request.task, "provider": request.provider_id, "intent": request.intent,
        "max_turns": request.max_turns,
    })
    return request


def _run(state: AppContext, request: TaskSubmission) -> None:
    ready, release = Event(), Event()
    release.set()
    provider = DelayedProvider(ready, release)

    def connect_provider(_provider_id: str) -> ChatProvider:
        return provider

    state.durable.claim_task(request.run_id)
    checkpoint = state.durable.task(request.run_id)
    assert checkpoint.phase == "dispatching"
    state.durable.start_task(request.run_id, state=dict(checkpoint.state))
    deps = build_task_deps(state, connect_provider=connect_provider)

    def execute() -> None:
        run_task_submission(deps, request)

    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(execute).result(timeout=20)
    assert ready.is_set() and state.wait_for_ghost_sleep(timeout=10)
    terminal: object = state.run_registry.last_terminal_event()
    assert mapping_fields(terminal) and terminal.get("stop_reason") == "done"


def _delete(home: Path, project: str, action: str, scope: str) -> GhostControlSurface:
    surface = GhostControlSurface.from_state_home(home)
    status, payload = surface.dispatch_action({
        "action": action, "scope": scope, "project": project,
        "session_id": "received-session", "confirm": True,
    })
    assert status == 200 and payload.get("ok") is True
    return surface


def _has_origin(home: Path, run_id: str) -> bool:
    with GhostSQLite(home).connection() as database:
        row: sqlite3.Row | None = database.execute(
            "SELECT run_id FROM ghost_input_origins WHERE run_id=?", (run_id,),
        ).fetchone()
    return row is not None


def _append(home: Path, run_id: str, project: str = "") -> bool:
    return GhostObservationStore(home).append_completed(
        run_id=run_id, session_id="received-session", project=project, mode="chat",
        user_text="received input", assistant_text="completed reply", stop_reason="done",
    )


@pytest.mark.parametrize("action,scope", [
    ("delete_scope", "user"), ("delete_scope", "project"),
    ("delete_scope", "session"), ("reset_all", "user"),
])
@pytest.mark.parametrize("reopen", [False, True])
def test_busy_queue_edit_and_placement_preserve_deleted_input_lifetime(
    tmp_path: Path, action: str, scope: str, reopen: bool,
) -> None:
    project = str(tmp_path / "project")
    with closing(AppContext(tmp_path, start_scheduler=False)) as state:
        _receive(state, project, run_id="blocker")
        request = _receive(state, project)
        checkpoint = state.durable.task(request.run_id)
        assert checkpoint.state.get("input_state") == "queued"
        assert _has_origin(tmp_path, request.run_id)
        surface = _delete(tmp_path, project, action, scope)
        assert surface.observations is not None and surface.continuity is not None
        assert not _has_origin(tmp_path, request.run_id)
        assert not surface.observations.read_all() and not surface.continuity.list_items()
        # Duplicate intake and ordinary queued revisions must not create a
        # fresh lifetime for an old input after delete/reset.
        _receive(state, project)
        assert not _has_origin(tmp_path, request.run_id)
        revised: object = _queue(state).edit(request.run_id, checkpoint.revision, request.task + " Please.")
        assert mapping_fields(revised) and revised.get("state") == "queued"
        if not reopen:
            _finish_queued(state, request, surface)
    if reopen:
        with closing(AppContext(tmp_path, start_scheduler=False)) as state:
            _finish_queued(state, request, GhostControlSurface.from_state_home(tmp_path))


def _finish_queued(state: AppContext, request: TaskSubmission, surface: GhostControlSurface) -> None:
    from dataclasses import replace

    assert surface.inbox is not None and surface.observations is not None and surface.continuity is not None
    assert surface.inbox.set_control("accept_events", True, scope="session", session_id=request.session_id)
    state.durable.request_cancel("blocker", reason="aborted")
    checkpoint = state.durable.task(request.run_id)
    state.durable.place_submission(checkpoint.submission_id)
    _run(state, replace(request, task=request.task + " Please."))
    assert not surface.observations.read_all()
    assert not surface.continuity.list_items()


def test_intake_receipt_precedes_canonical_commit_and_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, release = Event(), Event()
    original_commit = SQLiteDurableStore.commit
    publications: list[bool] = []

    def blocked_commit(store: SQLiteDurableStore, mutation: DurableMutation) -> CommitResult:
        if mutation.task is not None and mutation.task.task_id == "received-run":
            entered.set()
            assert release.wait(10)
        return original_commit(store, mutation)

    def publish(_result: CommitResult, mutation: DurableMutation) -> None:
        if mutation.task is not None:
            publications.append(not _has_origin(tmp_path, mutation.task.task_id))

    monkeypatch.setattr(SQLiteDurableStore, "commit", blocked_commit)
    services = DurableAppServices.open(
        tmp_path / "durable.sqlite3", publish=publish, dispatch=lambda _task: True, start=False,
    )
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(
                services.session.admit_input, conversation_id="received-session", request_id="received-input",
                task_id="received-run", text="received input", provider_snapshot={},
            )
            try:
                assert entered.wait(10)
                assert _has_origin(tmp_path, "received-run")
                # Read the store directly: the session mutation line is
                # intentionally held by intake until its commit returns.
                with pytest.raises(KeyError):
                    services.session.store.task("received-run")
                _delete(tmp_path, "", "delete_scope", "session")
            finally:
                release.set()
            pending.result(timeout=10)
        assert publications == [True]
        assert services.session.task("received-run").phase == "accepted"
        assert not _append(tmp_path, "received-run")
        # A bound runtime requires the new input's canonical admission too;
        # a bare projection write is no longer proof of a new accepted task.
        services.session.admit_input(
            conversation_id="received-session", request_id="new-input", task_id="new-run",
            text="new input", provider_snapshot={},
        )
        assert _append(tmp_path, "new-run")
    finally:
        assert services.close()


def test_intake_single_storage_failure_quarantines_ghost_without_cancelling_task(tmp_path: Path) -> None:
    database = GhostSQLite(tmp_path)
    with database.connection() as connection:
        connection.execute(
            "CREATE TRIGGER reject_intake_origin BEFORE INSERT ON ghost_input_origins "
            "BEGIN SELECT RAISE(ABORT,'fixture origin failure'); END",
        )
    with closing(AppContext(tmp_path, start_scheduler=False)) as state:
        request = _receive(state, "")
        assert state.durable.task(request.run_id).phase == "accepted"
        assert not _has_origin(tmp_path, request.run_id)
        with database.connection() as connection:
            connection.execute("DROP TRIGGER reject_intake_origin")
        _run(state, request)
        surface = GhostControlSurface.from_state_home(tmp_path)
        assert surface.observations is not None and surface.continuity is not None
        assert not surface.observations.read_all() and not surface.continuity.list_items()


@pytest.mark.parametrize("reopen", [False, True])
def test_delete_preserves_new_input_learning(tmp_path: Path, reopen: bool) -> None:
    project = str(tmp_path / "project")
    with closing(AppContext(tmp_path, start_scheduler=False)) as state:
        request = _receive(state, project)
        _delete(tmp_path, project, "delete_scope", "session")
        assert not _has_origin(tmp_path, request.run_id)
        state.durable.request_cancel(request.run_id, reason="aborted")
        if not reopen:
            _run_new(state, tmp_path, project)
    if reopen:
        with closing(AppContext(tmp_path, start_scheduler=False)) as state:
            _run_new(state, tmp_path, project)


def _run_new(state: AppContext, home: Path, project: str) -> None:
    request = _receive(state, project, run_id="new-run")
    assert _has_origin(home, request.run_id)
    _run(state, request)
    surface = GhostControlSurface.from_state_home(home)
    assert surface.observations is not None and surface.continuity is not None
    assert [row.get("run_id") for row in surface.observations.read_all()] == [request.run_id]
    assert any(item.source_ref == request.run_id for item in surface.continuity.list_items())


def test_core_admission_hook_runs_only_for_new_tasks_and_before_publication(tmp_path: Path) -> None:
    events: list[str] = []

    def before(task: TaskCheckpoint) -> None:
        events.append("before:" + task.task_id)

    def publish(_result: CommitResult, _mutation: DurableMutation) -> None:
        events.append("publish")

    with closing(DurableSession(
        SQLiteDurableStore(tmp_path / "durable.sqlite3"), on_commit=publish, before_admit=before,
    )) as session:
        arguments: dict[str, object] = {"project": ""}
        session.admit_input(
            conversation_id="session", request_id="request", task_id="task", text="input",
            provider_snapshot={}, task_state=arguments,
        )
        assert events == ["before:task", "publish"]
        session.admit_input(
            conversation_id="session", request_id="request", task_id="task", text="input",
            provider_snapshot={}, task_state=arguments,
        )
        assert events == ["before:task", "publish"]
        session.claim_task("task")
        assert events == ["before:task", "publish", "publish"]
    assert not (tmp_path / "ghost.sqlite3").exists()


def test_ephemeral_app_keeps_receipts_out_of_user_memory() -> None:
    with closing(AppContext(None, start_scheduler=False)) as state:
        _receive(state, "")
        assert state.state_home is None
        assert state.ghost_observations is None and state.ghost_inbox is None
        assert state.durable.task("received-run").phase == "accepted"


@pytest.mark.parametrize("scope", ["session", "project"])
def test_intake_scope_registration_preserves_unrelated_inputs(tmp_path: Path, scope: str) -> None:
    project = str(tmp_path / "project")
    other_session = "other-session" if scope == "session" else "received-session"
    other_project = str(tmp_path / "other-project") if scope == "project" else project
    with closing(AppContext(tmp_path, start_scheduler=False)) as state:
        _receive(state, project)
        state.durable.admit_input(
            conversation_id=other_session, request_id="other-input", task_id="other-run", text="other input",
            provider_snapshot={}, task_state={"project": other_project},
        )
        surface = _delete(tmp_path, project, "delete_scope", scope)
        assert not _has_origin(tmp_path, "received-run")
        assert _has_origin(tmp_path, "other-run")
        assert surface.observations is not None
        assert not _append(tmp_path, "received-run", project)
        assert surface.observations.append_completed(
            run_id="other-run", session_id=other_session, project=other_project, mode="chat",
            user_text="other input", assistant_text="other reply", stop_reason="done",
        )


def test_normal_run_submission_registers_before_worker_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codey.app import task_submit

    reached: list[bool] = []

    def decline_worker(*_args: object, **_kwargs: object) -> bool:
        reached.append(_has_origin(tmp_path, "received-run"))
        _delete(tmp_path, "", "delete_scope", "session")
        return False

    monkeypatch.setattr(task_submit, "submit_browser_task", decline_worker)
    with closing(AppContext(tmp_path, start_scheduler=False)) as state:
        run_id = task_submit.submit_task(
            "received-session", None, "What needs verification?", 2, False, "deepseek", "chat",
            get_state=lambda: state, run_id="received-run",
        )
        assert run_id == "received-run" and reached == [True]
        assert state.durable.task(run_id).phase == "accepted"
        _run(state, TaskSubmission(
            "received-session", None, "What needs verification?", 2, False, "deepseek",
            intent="chat", run_id=run_id,
        ))
        surface = GhostControlSurface.from_state_home(tmp_path)
        assert surface.observations is not None and surface.continuity is not None
        assert not surface.observations.read_all() and not surface.continuity.list_items()


def test_headless_admission_registers_before_operation_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ready, release = Event(), Event()
    release.set()
    provider = DelayedProvider(ready, release)
    events: list[dict[str, object]] = []

    def connect(_provider_id: str, *, port: int) -> ChatProvider:
        return provider

    def enter(deps: TaskRunDeps, request: TaskSubmission) -> None:
        assert _has_origin(tmp_path, request.run_id)
        assert not ready.is_set()
        _delete(tmp_path, "", "delete_scope", "session")
        run_task_submission(deps, request)

    monkeypatch.setattr("codey.app.headless_runner.run_task_submission", enter)
    result = run_headless(
        HeadlessRequest(
            project=None, task="What needs verification?", intent="chat", provider_id="deepseek",
            run_id="received-run", session_id="received-session", state_home=tmp_path, max_turns=2,
        ),
        emit_jsonl=events.append, connect_provider=connect,
    )
    assert ready.is_set() and result.stop_reason == "done" and result.exit_code == 0
    assert any(event.get("type") == "task_done" and event.get("stop_reason") == "done" for event in events)
    surface = GhostControlSurface.from_state_home(tmp_path)
    assert surface.observations is not None and surface.continuity is not None
    assert not surface.observations.read_all() and not surface.continuity.list_items()


def _crash_before_canonical_commit(home: Path) -> None:
    original = SQLiteDurableStore.commit

    def crash(store: SQLiteDurableStore, mutation: DurableMutation) -> CommitResult:
        if mutation.task is not None and mutation.task.task_id == "received-run":
            os._exit(23)
        return original(store, mutation)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(SQLiteDurableStore, "commit", crash)
        with closing(AppContext(home, start_scheduler=False)) as state:
            _receive(state, "")


def test_crash_after_receipt_before_canonical_commit_does_not_resurrect_input(tmp_path: Path) -> None:
    process = multiprocessing.get_context("spawn").Process(target=_crash_before_canonical_commit, args=(tmp_path,))
    process.start()
    process.join(20)
    if process.is_alive():
        process.terminate()
        process.join(5)
        pytest.fail("intake crash fixture did not finish")
    assert process.exitcode == 23
    assert _has_origin(tmp_path, "received-run")
    with closing(AppContext(tmp_path, start_scheduler=False)) as state:
        with pytest.raises(KeyError):
            state.durable.task("received-run")
        surface = _delete(tmp_path, "", "delete_scope", "session")
        request = _receive(state, "")
        assert not _has_origin(tmp_path, request.run_id)
        _run(state, request)
        assert surface.observations is not None and surface.continuity is not None
        assert not surface.observations.read_all() and not surface.continuity.list_items()
