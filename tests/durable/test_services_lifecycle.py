from __future__ import annotations

import threading
from pathlib import Path

import pytest

from codey.app.durable_services import DurableAppServices
from codey.durable import DurableSession, DurableTaskScheduler, SQLiteDurableStore
from codey.durable.errors import SchemaVersionError
from codey.storage.file_lock import FileLease, LockTimeout, acquire_lease


def _assert_ownership_released(services: DurableAppServices) -> None:
    assert services._database_lease is None


def test_durable_services_close_scheduler_before_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    services = DurableAppServices.open(
        tmp_path / "durable.sqlite3", publish=None, dispatch=lambda _checkpoint: True, start=False,
    )
    close_scheduler = services.scheduler.close
    close_session = services.session.close

    def scheduler_close(timeout: float = 5.0) -> bool:
        events.append("scheduler")
        return close_scheduler(timeout)

    def session_close() -> None:
        events.append("session")
        close_session()

    monkeypatch.setattr(services.scheduler, "close", scheduler_close)
    monkeypatch.setattr(services.session, "close", session_close)
    try:
        assert services.close()
    finally:
        services.close()
    assert events == ["scheduler", "session"]


def test_durable_services_open_owns_real_durable_pair(tmp_path: Path) -> None:
    services = DurableAppServices.open(
        tmp_path / "durable.sqlite3",
        publish=lambda _result, _mutation: None,
        dispatch=lambda _checkpoint: True,
        start=False,
    )
    try:
        store = services.session.store
        assert isinstance(store, SQLiteDurableStore)
        assert store.path == tmp_path / "durable.sqlite3"
        assert services.scheduler is not None
    finally:
        assert services.close()


def test_durable_services_fences_second_open_of_same_database(tmp_path: Path) -> None:
    path = tmp_path / "durable.sqlite3"
    first = DurableAppServices.open(
        path, publish=None, dispatch=lambda _checkpoint: True, start=False,
    )
    try:
        with pytest.raises(LockTimeout):
            DurableAppServices.open(
                path, publish=None, dispatch=lambda _checkpoint: True, start=False,
            )
    finally:
        assert first.close()

    reopened = DurableAppServices.open(
        path, publish=None, dispatch=lambda _checkpoint: True, start=False,
    )
    assert reopened.close()


def test_durable_ownership_is_retained_until_scheduler_cleanup_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "durable.sqlite3"
    services = DurableAppServices.open(path, publish=None, dispatch=lambda _checkpoint: True, start=False)
    close = services.scheduler.close
    attempts = 0

    def busy_once(timeout: float = 5.0) -> bool:
        nonlocal attempts
        attempts += 1
        return False if attempts == 1 else close(timeout)

    monkeypatch.setattr(services.scheduler, "close", busy_once)
    try:
        assert not services.close()
        assert not services._session_closed
        assert services._database_lease is not None
        with pytest.raises(LockTimeout):
            acquire_lease(path)
        assert services.close()
        _assert_ownership_released(services)
        with acquire_lease(path):
            pass
    finally:
        assert services.close()


def test_durable_services_rejects_incompatible_database_without_deleting_it(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "durable.sqlite3"
    with sqlite3.connect(path) as database:
        database.execute("PRAGMA user_version=2")
        database.execute("CREATE TABLE sentinel(value TEXT NOT NULL)")
        database.execute("INSERT INTO sentinel(value) VALUES('keep')")

    with pytest.raises(SchemaVersionError):
        DurableAppServices.open(
            path, publish=None, dispatch=lambda _checkpoint: True, start=False,
        )

    with sqlite3.connect(path) as database:
        row: object = database.execute("SELECT value FROM sentinel").fetchone()
        assert row == ("keep",)


def test_failed_scheduler_thread_start_releases_the_durable_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[DurableAppServices] = []

    class ObservedServices(DurableAppServices):
        def __init__(
            self, session: DurableSession, scheduler: DurableTaskScheduler,
            _scheduler_closed: bool = False, _session_closed: bool = False,
            _database_lease: FileLease | None = None,
        ) -> None:
            super().__init__(session, scheduler, _scheduler_closed, _session_closed, _database_lease)
            created.append(self)

    def unavailable(thread: threading.Thread) -> None:
        assert thread.name == "codey-durable-scheduler"
        raise RuntimeError("dispatcher thread unavailable")

    monkeypatch.setattr("threading.Thread.start", unavailable)
    path = tmp_path / "durable.sqlite3"
    seed = DurableAppServices.open(path, publish=None, dispatch=lambda _checkpoint: True, start=False)
    try:
        seed.session.admit_input(
            conversation_id="c", request_id="input", task_id="work", text="recover pending work", provider_snapshot={},
        )
    finally:
        assert seed.close()
    try:
        with pytest.raises(RuntimeError, match="dispatcher thread unavailable"):
            ObservedServices.open(path, publish=None, dispatch=lambda _checkpoint: True)
        assert len(created) == 1
        failed = created[0]
        assert failed._session_closed, "failed startup left its durable session open"
        assert failed._scheduler_closed
        assert failed._database_lease is None
        assert path.is_file()
        with acquire_lease(path):
            pass
        reopened = DurableAppServices.open(path, publish=None, dispatch=lambda _checkpoint: True, start=False)
        assert reopened.session.task("work").conversation_id == "c"
        assert reopened.close()
    finally:
        for services in created:
            assert services.close()
