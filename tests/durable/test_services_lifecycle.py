from __future__ import annotations

from pathlib import Path

import pytest

from codey.app.durable_services import DurableAppServices
from codey.storage.file_lock import LockTimeout


def test_durable_services_close_scheduler_before_session(tmp_path: Path) -> None:
    events: list[str] = []

    class Session:
        store = None

        def close(self) -> None:
            events.append("session")

    class Scheduler:
        def close(self, _timeout: float = 5.0) -> bool:
            events.append("scheduler")
            return True

    services = DurableAppServices(Session(), Scheduler())  # type: ignore[arg-type]
    assert services.close()
    assert events == ["scheduler", "session"]


def test_durable_services_open_owns_real_durable_pair(tmp_path: Path) -> None:
    services = DurableAppServices.open(
        tmp_path / "durable.sqlite3",
        publish=lambda _result, _mutation: None,
        dispatch=lambda _checkpoint: True,
        start=False,
    )
    try:
        assert services.session.store.path == tmp_path / "durable.sqlite3"
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
