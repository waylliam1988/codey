"""Explicit offline import of pre-durable chat history; never used at startup.

Run with ``python -m tools.rescue_legacy_chats STATE_HOME``. Only an empty
version-4 development database may be replaced. A fresh version-1 database is
built and checked before installation. No backup or executable task is made.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
from contextlib import closing
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from codey.agents.handoff import ConversationSnapshot
from codey.durable import DurableSession, SQLiteDurableStore
from codey.durable.model import DurableMutation, EntryDraft, thaw_json
from codey.storage.file_lock import acquire_lease
from codey.storage.local_store import session_key, write_json_atomic

IMPORT_MARKER = "legacy_chat_rescue"


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path.name}")
    return value


def _identity(message: dict[str, Any]) -> str:
    if message.get("streamId"):
        return f"stream:{message['streamId']}"
    return str(message.get("id") or message.get("eventKey") or "")


def _match_key(message: dict[str, Any]) -> str:
    identity = _identity(message)
    if identity:
        return "identity:" + identity
    # Matching full source records (including turn/request identity), with
    # occurrence counts preserved, is distinct from deduplicating body text.
    return "record:" + json.dumps(message, ensure_ascii=False, sort_keys=True)


def _merge(archive: list[dict[str, Any]], ui: list[dict[str, Any]]) -> list[dict[str, Any]]:
    matcher = SequenceMatcher(a=[_match_key(m) for m in archive], b=[_match_key(m) for m in ui], autojunk=False)
    result: list[dict[str, Any]] = []
    for tag, a, b, c, d in matcher.get_opcodes():
        if tag == "equal":
            for old, visible in zip(archive[a:b], ui[c:d], strict=True):
                # The archive may have received newer stream parts after the
                # last UI save. Do not replace its later complete response.
                if int(visible.get("streamSequence") or 0) > int(old.get("streamSequence") or 0):
                    result.append({**old, **visible})
                else:
                    result.append({**visible, **old})
        else:
            result.extend(archive[a:b])
            result.extend(ui[c:d])
    return result


def _materialize(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep each identified message's latest revision, including stream parts."""
    result: list[dict[str, Any]] = []
    positions: dict[tuple[str, str], int] = {}
    for message in messages:
        identity = _identity(message)
        key = (str(message.get("type")), identity)
        if identity and key in positions:
            index = positions[key]
            previous = result[index]
            if int(message.get("streamSequence") or 0) >= int(previous.get("streamSequence") or 0):
                result[index] = {**previous, **message}
        else:
            if identity:
                positions[key] = len(result)
            result.append(message)
    return result


def _sources(root: Path) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    state = _read_json(root / "ui-state.json")["state"]
    chats: dict[str, list[dict[str, Any]]] = {}
    tombstones: dict[str, set[str]] = {}
    barriers: dict[str, int] = {}
    with closing(sqlite3.connect(f"{(root / 'transcript.sqlite3').as_uri()}?mode=ro", uri=True)) as db:
        db.execute("BEGIN")
        for session_id, identity in db.execute("SELECT session_id, identity FROM transcript_tombstones"):
            tombstones.setdefault(session_id, set()).add(identity)
        barriers.update(db.execute("SELECT session_id, revision FROM transcript_ui_barriers"))
        rows = db.execute("""SELECT m.session_id,m.message_id,m.payload FROM transcript_messages m
            JOIN transcript_sessions s ON m.session_id=s.session_id AND m.epoch=s.epoch
            ORDER BY m.session_id,m.ordinal""")
        for session_id, message_id, payload in rows:
            message = json.loads(payload)
            if not isinstance(message, dict):
                raise ValueError("invalid archived message")
            deleted = tombstones.get(session_id, set())
            if message_id not in deleted and _identity(message) not in deleted:
                chats.setdefault(session_id, []).append(message)
    for card in state["sessions"]:
        session_id = card["id"]
        if int(state.get("revision") or 0) <= barriers.get(session_id, -1):
            continue
        messages = [m for m in card.get("messages", []) if _identity(m) not in tombstones.get(session_id, set())]
        if messages:
            chats[session_id] = _merge(chats.get(session_id, []), messages)
    return state, {session_id: _materialize(messages) for session_id, messages in chats.items()}


def _kind(message: dict[str, Any]) -> str:
    if message.get("type") == "user":
        return "user_message"
    if (message.get("type") == "asst" and message.get("finalAnswer") is not False
            and message.get("contentRole") not in {"progress", "commentary"}
            and message.get("streamState") not in {"streaming", "partial", "interrupted", "error"}):
        return "assistant_message"
    return "display_message"


def _build(root: Path, path: Path, chats: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    session = DurableSession(SQLiteDurableStore(path))
    report: dict[str, Any] = {"schema_version": 1, "conversations": len(chats), "messages": 0, "snapshots": 0,
                              "session_ids": sorted(chats)}
    for session_id, messages in chats.items():
        session.create_conversation(session_id)
        entries: list[EntryDraft] = []
        parent_id = None
        for index, message in enumerate(messages):
            key = json.dumps([session_id, index, _match_key(message)], ensure_ascii=False)
            entry_id = "import-" + hashlib.sha256(key.encode("utf-8")).hexdigest()
            entries.append(EntryDraft(entry_id, session_id, parent_id, _kind(message), message))
            parent_id = entry_id
        snapshot_path = root / "conversations" / f"{session_key(session_id)}.json"
        if snapshot_path.is_file():
            snapshot = ConversationSnapshot.from_payload(_read_json(snapshot_path)["snapshot"])
            entries.append(EntryDraft(f"snapshot-{session_key(session_id)}", session_id, parent_id,
                                      "context_snapshot", snapshot.to_payload()))
            report["snapshots"] += 1
        session._commit(DurableMutation(session_id, 0, entries=tuple(entries)))
        restored = session.history_page(session_id, limit=200)
        stored = session.entries(session_id)
        assert len(stored) == len(entries)
        assert all(thaw_json(actual.payload) == thaw_json(expected.payload)
                   for actual, expected in zip(stored, entries, strict=True))
        assert restored["items"] or not messages
        report["messages"] += len(messages)
    session.close()
    with closing(sqlite3.connect(path)) as db:
        db.execute("INSERT INTO schema_metadata(key,value) VALUES(?,?)", (IMPORT_MARKER, json.dumps(report)))
        db.commit()
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or db.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("imported database validation failed")
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.execute("PRAGMA journal_mode=DELETE")
    return report


def _check_target(path: Path) -> dict[str, Any] | None:
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as db:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version == 1:
            marker = db.execute("SELECT value FROM schema_metadata WHERE key=?", (IMPORT_MARKER,)).fetchone()
            if marker:
                return json.loads(marker[0])
        if version != 4:
            raise ValueError("rescue requires the empty version-4 development database")
        names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        for name in names:
            if name == "schema_migrations":
                continue
            quoted = '"' + name.replace('"', '""') + '"'
            if db.execute(f"SELECT 1 FROM {quoted} LIMIT 1").fetchone():
                raise ValueError("durable database is not empty; refusing replacement")
    return None


def _finish_ui(root: Path, report: dict[str, Any]) -> None:
    envelope = _read_json(root / "ui-state.json")
    state = envelope["state"]
    cards = state["sessions"]
    ids = {card["id"] for card in cards}
    session = DurableSession(SQLiteDurableStore(root / "durable.sqlite3"))
    for session_id in report["session_ids"]:
        if session_id not in ids:
            entries = session.entries(session_id)
            title = next((str(e.payload["text"])[:160] for e in entries if e.kind == "user_message"), "Imported chat")
            cards.append({"id": session_id, "title": title, "createdAt": 0, "provider": "deepseek"})
    session.close()
    for card in cards:
        card.pop("messages", None)
    if envelope != _read_json(root / "ui-state.json"):
        state["revision"] = int(state.get("revision") or 0) + 1
        write_json_atomic(root / "ui-state.json", envelope, max_bytes=16 * 1024 * 1024)


def rescue(state_home: Path) -> dict[str, Any]:
    root = state_home.resolve(strict=True)
    path = root / "durable.sqlite3"
    lease = acquire_lease(root / ".server.lock", timeout_seconds=0.0)
    try:
        previous = _check_target(path)
        if previous is not None:
            _finish_ui(root, previous)
            return previous
        _state, chats = _sources(root)
        if not chats:
            raise ValueError("no legacy chat history found")
        # Staging is the new database, not a backup. The original empty DB and
        # UI remain untouched if any import or validation step fails.
        with tempfile.TemporaryDirectory(prefix=".chat-rescue-", dir=root) as staging:
            staged_path = Path(staging) / "durable.sqlite3"
            report = _build(root, staged_path, chats)
            _check_target(path)
            with closing(sqlite3.connect(path)) as db:
                db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                db.execute("PRAGMA journal_mode=DELETE")
            os.replace(staged_path, path)
        _finish_ui(root, report)
        return report
    finally:
        lease.release()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state_home", type=Path)
    print(json.dumps(rescue(parser.parse_args().state_home), ensure_ascii=False))
