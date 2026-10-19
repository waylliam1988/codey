from __future__ import annotations

import sqlite3
from pathlib import Path

from codey.ghost.observations import GhostObservationStore


def test_connection_configuration_failure_closes_the_handle(tmp_path: Path, monkeypatch) -> None:
    import pytest

    from codey.ghost.sqlite import GhostSQLite

    closed = []
    connect = sqlite3.connect

    class Connection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if sql == "PRAGMA synchronous=FULL":
                raise sqlite3.OperationalError("injected configuration failure")
            return super().execute(sql, parameters)

        def close(self):
            closed.append(True)
            super().close()

    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: connect(*args, **kwargs, factory=Connection))
    with pytest.raises(sqlite3.OperationalError, match="injected"):
        GhostSQLite(tmp_path)
    assert closed == [True]


def test_concurrent_first_open_preserves_all_writes(tmp_path: Path) -> None:
    import threading
    from concurrent.futures import ThreadPoolExecutor

    barrier = threading.Barrier(8)

    def append(index):
        barrier.wait(timeout=10)
        _append(GhostObservationStore(tmp_path), f"concurrent-{index}")

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(append, range(8)))
    assert len(GhostObservationStore(tmp_path).read_all()) == 8


def _append(store: GhostObservationStore, run_id: str, *, session_id: str = "s") -> None:
    assert store.append_completed(
        run_id=run_id,
        session_id=session_id,
        mode="chat",
        user_text="hello",
        assistant_text="hi",
        stop_reason="done",
        provider_id="local",
    )


def test_observations_use_the_independent_ghost_sqlite_database(tmp_path: Path) -> None:
    store = GhostObservationStore(tmp_path)

    assert store.database_path == tmp_path / "ghost.sqlite3"
    assert store.database_path.is_file()
    assert not (tmp_path / "ghost" / "observations.jsonl").exists()

    _append(store, "run-1")
    assert GhostObservationStore(tmp_path).read_committed(session_id="s")

    with sqlite3.connect(store.database_path) as database:
        assert database.execute(
            "SELECT COUNT(*) FROM observation_events WHERE run_id=?", ("run-1",)
        ).fetchone() == (1,)


def test_observation_scope_mutations_are_sqlite_transactions(tmp_path: Path) -> None:
    store = GhostObservationStore(tmp_path)
    _append(store, "run-a", session_id="s-a")
    _append(store, "run-b", session_id="s-b")

    assert store.delete_scope("session", session_id="s-a") == 1
    assert [row["run_id"] for row in store.read_all()] == ["run-b"]

    store.reset_all()
    assert store.read_all() == ()
