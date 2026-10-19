from __future__ import annotations

import sqlite3
from pathlib import Path

from codey.ghost.observations import GhostObservationStore


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
