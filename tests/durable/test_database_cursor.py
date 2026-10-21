import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from codey.durable import DurableSession, MemoryDurableStore, SQLiteDurableStore


def test_database_identity_survives_reopen_and_separates_independent_databases(tmp_path: Path) -> None:
    path = tmp_path / "durable.sqlite3"
    first = DurableSession(SQLiteDurableStore(path))
    identity = first.database_id
    assert identity and DurableSession(SQLiteDurableStore(path)).database_id == identity
    assert DurableSession(SQLiteDurableStore(tmp_path / "other.sqlite3")).database_id != identity


def test_concurrent_cold_start_serializes_schema_initialization(tmp_path: Path) -> None:
    path = tmp_path / "durable.sqlite3"

    def open_store(_index: int) -> str:
        return SQLiteDurableStore(path).database_id

    with ThreadPoolExecutor(max_workers=8) as pool:
        identities = list(pool.map(open_store, range(8)))
    assert identities and all(identity == identities[0] for identity in identities)


def test_cold_start_serializes_the_wal_change_before_schema_transactions(tmp_path, monkeypatch):
    first_entered, release, second_entered = threading.Event(), threading.Event(), threading.Event()
    calls = []

    def initialize(connect):
        calls.append(True)
        if len(calls) == 1:
            first_entered.set()
            assert release.wait(10)
        else:
            second_entered.set()
        return "database-fixture"

    monkeypatch.setattr("codey.durable.sqlite.initialize_database", initialize)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(SQLiteDurableStore, tmp_path / "durable.sqlite3")
        assert first_entered.wait(5)
        second = pool.submit(SQLiteDurableStore, tmp_path / "durable.sqlite3")
        try:
            assert not second_entered.wait(0.2), "A second initializer entered before the first WAL transition finished"
        finally:
            release.set()
        assert first.result().database_id == second.result().database_id


def test_history_and_replayed_events_use_the_persisted_database_identity(tmp_path: Path) -> None:
    session = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    session.create_conversation("chat")
    session.append_message("chat", "user_message", {"text": "hello"})
    assert session.history_page("chat")["epoch"] == session.database_id
    events = session.commits_after(0)
    assert [event.sequence for event in events] == [1, 2]
    assert all(event.to_payload()["database_id"] == session.database_id for event in events)


@pytest.mark.parametrize("scope,after", [("other-database", 1), (None, 1), ("current", 500), ("current", 1)])
def test_sse_reconciles_database_reset_or_cursor_ahead_without_skipping_commits(scope, after) -> None:
    from tests.sse_http import capture_events

    session = DurableSession(MemoryDurableStore())
    session.create_conversation("chat")
    for text in ("a", "b"):
        session.append_message("chat", "user_message", {"text": text})
    identity = session.database_id if scope == "current" else scope
    written = capture_events(session, f"/api/events?after_commit_seq={after}" + (f"&database_id={identity}" if identity else ""))
    assert written[0]["database_id"] == session.database_id
    replayed = [item["commit_seq"] for item in written if item["type"] == "durable_commit"]
    assert replayed == ([2, 3] if scope == "current" and after == 1 else [1, 2, 3])
