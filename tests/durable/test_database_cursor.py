from pathlib import Path
from queue import Queue
from types import SimpleNamespace

import pytest

from codey.durable import DurableSession, MemoryDurableStore, SQLiteDurableStore


def test_database_identity_survives_reopen_and_separates_independent_databases(tmp_path: Path) -> None:
    path = tmp_path / "durable.sqlite3"
    first = DurableSession(SQLiteDurableStore(path))
    identity = first.database_id
    assert identity and DurableSession(SQLiteDurableStore(path)).database_id == identity
    assert DurableSession(SQLiteDurableStore(tmp_path / "other.sqlite3")).database_id != identity


def test_history_and_replayed_events_use_the_persisted_database_identity(tmp_path: Path) -> None:
    session = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    session.create_conversation("chat")
    session.append_message("chat", "user_message", {"text": "hello"})
    assert session.history_page("chat")["epoch"] == session.database_id
    events = session.commits_after(0)
    assert [event.sequence for event in events] == [1, 2]
    assert all(event.to_payload()["database_id"] == session.database_id for event in events)


@pytest.mark.parametrize("scope,after", [("other-database", 1), (None, 1), ("current", 500), ("current", 1)])
def test_sse_reconciles_database_reset_or_cursor_ahead_without_skipping_commits(monkeypatch, scope, after) -> None:
    from codey.app import server

    session = DurableSession(MemoryDurableStore())
    session.create_conversation("chat")
    for text in ("a", "b"):
        session.append_message("chat", "user_message", {"text": text})
    events = Queue()
    events.replay_cutoff = 7
    events.put({"type": "end"})
    state = SimpleNamespace(durable=session, run_status=lambda: {}, subscribe=lambda: events, unsubscribe=lambda _q: None)
    monkeypatch.setattr(server, "get_state", lambda: state)
    written = []

    def write(event, **_kwargs):
        written.append(event)
        return event["type"] != "end"

    identity = session.database_id if scope == "current" else scope
    handler = SimpleNamespace(path=f"/api/events?after_commit_seq={after}" + (f"&database_id={identity}" if identity else ""),
        headers={}, send_response=lambda *_args: None, send_header=lambda *_args: None,
        end_headers=lambda: None, _write_sse_event=write)
    server.Handler._sse(handler)
    assert written[0]["database_id"] == session.database_id
    replayed = [item["commit_seq"] for item in written if item["type"] == "durable_commit"]
    assert replayed == ([2, 3] if scope == "current" and after == 1 else [1, 2, 3])
