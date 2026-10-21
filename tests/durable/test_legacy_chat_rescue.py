"""An explicit one-time rescue never becomes a production startup fallback."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock

import pytest

from codey.durable import DurableSession, SQLiteDurableStore
from codey.durable.errors import SchemaVersionError


def seed_legacy(root: Path) -> None:
    root.mkdir(exist_ok=True)
    with closing(sqlite3.connect(root / "durable.sqlite3")) as db, db:
        db.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY)")
        db.execute("PRAGMA user_version=4")
    with closing(sqlite3.connect(root / "transcript.sqlite3")) as db, db:
        db.executescript("""
            CREATE TABLE transcript_sessions(session_id TEXT, epoch TEXT);
            CREATE TABLE transcript_messages(session_id TEXT, epoch TEXT, message_id TEXT, ordinal INTEGER, payload TEXT);
            CREATE TABLE transcript_tombstones(session_id TEXT, identity TEXT);
            CREATE TABLE transcript_ui_barriers(session_id TEXT, revision INTEGER);
        """)
        db.executemany("INSERT INTO transcript_sessions VALUES(?,?)", [("chat", "current"), ("cleared", "new")])
        messages = [
            ("chat", "old", "stale", 1, {"type": "user", "text": "old epoch must stay deleted"}),
            ("chat", "current", "u1", 1, {"type": "user", "id": "u1", "text": "重复输入"}),
            ("chat", "current", "u2", 2, {"type": "user", "id": "u2", "text": "重复输入"}),
            ("chat", "current", "event:partial", 3, {"type": "asst", "streamId": "reply", "streamSequence": 2,
                                                        "text": "完整", "finalAnswer": False}),
            ("chat", "current", "stream:reply", 4, {"type": "asst", "streamId": "reply", "streamSequence": 5,
                                                      "text": "完整回答", "finalAnswer": True}),
            ("chat", "current", "deleted", 5, {"type": "asst", "text": "tombstoned"}),
        ]
        db.executemany("INSERT INTO transcript_messages VALUES(?,?,?,?,?)",
                       [(s, e, i, o, json.dumps(m)) for s, e, i, o, m in messages])
        db.execute("INSERT INTO transcript_tombstones VALUES('chat','deleted')")
        db.execute("INSERT INTO transcript_ui_barriers VALUES('cleared',7)")
    ui = {"schema_version": 1, "state": {"revision": 6, "active_id": "chat", "projects": [], "sessions": [
        {"id": "chat", "title": "保留标题", "draft": {"text": "未发送"}, "messages": [
            {"type": "user", "id": "u1", "text": "重复输入"},
            {"type": "user", "id": "u2", "text": "重复输入"},
            {"type": "asst", "streamId": "reply", "streamSequence": 3, "text": "完整回", "finalAnswer": False},
            {"type": "shell_result", "output": "历史命令结果"},
        ]},
        {"id": "cleared", "messages": [{"type": "user", "text": "must not resurrect"}]},
    ]}}
    (root / "ui-state.json").write_text(json.dumps(ui), encoding="utf-8")


def test_explicit_rescue_preserves_history_and_does_not_create_executable_tasks(tmp_path: Path) -> None:
    from tools.rescue_legacy_chats import rescue

    seed_legacy(tmp_path)
    report = rescue(tmp_path)
    session = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    entries = session.entries("chat")
    assert [entry.payload.get("text") for entry in entries if entry.kind == "user_message"] == ["重复输入", "重复输入"]
    assert len(entries) == 4
    assert any(entry.payload.get("output") == "历史命令结果" for entry in entries)
    assert session.pending_tasks() == ()
    assert not session.has_conversation("cleared")
    messages = session.context_snapshot("chat", token_budget=10000).messages
    assert [message["content"] for message in messages] == ["重复输入", "重复输入", "完整回答"]
    assert report["messages"] == 4
    ui = json.loads((tmp_path / "ui-state.json").read_text(encoding="utf-8"))["state"]
    assert ui["sessions"][0]["title"] == "保留标题"
    assert ui["sessions"][0]["draft"] == {"text": "未发送"}
    assert all("messages" not in item for item in ui["sessions"])
    with closing(sqlite3.connect(tmp_path / "durable.sqlite3")) as db, db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    before = session.entries("chat")
    assert rescue(tmp_path) == report
    assert session.entries("chat") == before


def test_rescue_refuses_to_replace_any_nonempty_durable_database(tmp_path: Path) -> None:
    from tools.rescue_legacy_chats import rescue

    seed_legacy(tmp_path)
    with closing(sqlite3.connect(tmp_path / "durable.sqlite3")) as db, db:
        db.execute("INSERT INTO tasks VALUES('keep-me')")
    original = (tmp_path / "durable.sqlite3").read_bytes()
    with pytest.raises(ValueError, match="not empty"):
        rescue(tmp_path)
    assert (tmp_path / "durable.sqlite3").read_bytes() == original


def test_failed_import_leaves_original_database_and_ui_intact(tmp_path: Path, monkeypatch) -> None:
    from tools.rescue_legacy_chats import rescue

    seed_legacy(tmp_path)
    before = {name: (tmp_path / name).read_bytes() for name in ("durable.sqlite3", "ui-state.json")}
    monkeypatch.setattr(SQLiteDurableStore, "commit", Mock(side_effect=OSError("injected write failure")))
    with pytest.raises(OSError, match="injected"):
        rescue(tmp_path)
    assert {name: (tmp_path / name).read_bytes() for name in before} == before
    assert not list(tmp_path.glob(".chat-rescue-*"))


def test_production_startup_still_rejects_legacy_schema(tmp_path: Path) -> None:
    seed_legacy(tmp_path)
    with pytest.raises(SchemaVersionError):
        SQLiteDurableStore(tmp_path / "durable.sqlite3")


def test_history_projection_encodes_nested_immutable_payloads_as_json(tmp_path: Path) -> None:
    session = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    session.create_conversation("chat")
    session.append_message("chat", "display_message", {
        "type": "shell_result", "audit": {"receipt": {"ok": True}}, "items": [{"name": "result"}],
    })
    page = json.loads(json.dumps(session.history_page("chat")))
    assert page["items"][0]["audit"] == {"receipt": {"ok": True}}
    assert page["items"][0]["items"] == [{"name": "result"}]


def test_startup_failure_never_announces_ui_ready(tmp_path: Path, monkeypatch, capsys) -> None:
    from codey.app.context import AppContext
    from codey.app.service import LocalService

    monkeypatch.setattr(AppContext, "__init__", Mock(side_effect=SchemaVersionError("newer schema")))
    with pytest.raises(SchemaVersionError):
        LocalService(tmp_path).start()
    assert "UI ready" not in capsys.readouterr().out
