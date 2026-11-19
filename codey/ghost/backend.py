"""Built-in memory implementation. All executable authority stays with the host."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime

from codey.ghost.retrieval import relevance
from codey.memory.contracts import (
    DeleteResult,
    DeleteScopeRequest,
    MemoryDecision,
    MemoryEvent,
    MemoryHost,
    MemoryItem,
    MemoryKind,
    MemoryProposal,
    MemoryQuery,
    MemoryResult,
)
from codey.types.json_value import JsonValue, decode_json_object, require_json_string, require_json_strings


def _item(value: object) -> MemoryItem:
    payload = decode_json_object(value)
    kind = payload.get("kind")
    if kind not in ("experience", "preference", "goal", "hypothesis"):
        raise ValueError("unknown memory kind")
    item_kind: MemoryKind
    if kind == "experience":
        item_kind = "experience"
    elif kind == "preference":
        item_kind = "preference"
    elif kind == "goal":
        item_kind = "goal"
    else:
        item_kind = "hypothesis"
    confidence = payload.get("confidence")
    if confidence is not None and (isinstance(confidence, bool) or not isinstance(confidence, (float, int))):
        raise ValueError("confidence must be numeric or null")
    return MemoryItem(
        item_id=require_json_string(payload.get("item_id"), "item_id"),
        kind=item_kind,
        text=require_json_string(payload.get("text"), "text"),
        scope=require_json_string(payload.get("scope"), "scope"),
        source_refs=tuple(require_json_strings(payload.get("source_refs"), "source_refs")),
        created_at=require_json_string(payload.get("created_at"), "created_at"),
        expires_at=require_json_string(payload.get("expires_at", ""), "expires_at"),
        confidence=float(confidence) if confidence is not None else None,
    )


def _payload(item: MemoryItem) -> str:
    return json.dumps(item.to_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _texts(cursor: sqlite3.Cursor) -> tuple[str, ...]:
    rows: list[object] = cursor.fetchall()
    result: list[str] = []
    for row in rows:
        if not isinstance(row, tuple):
            raise ValueError("invalid memory database row")
        cells: tuple[object, ...] = row
        if len(cells) != 1 or not isinstance(cells[0], str):
            raise ValueError("invalid memory database payload")
        result.append(cells[0])
    return tuple(result)


class GhostMemory:
    def __init__(self, host: MemoryHost) -> None:
        self._host = host
        self._path = host.state_dir / "memory.sqlite3"

    @contextmanager
    def _connect(self, *, write: bool = False) -> Generator[sqlite3.Connection, None, None]:
        self._host.check_cancelled()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._path, timeout=0.05)
        try:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS events (event_id TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS items (
                    item_id TEXT PRIMARY KEY, scope TEXT NOT NULL, run_id TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS item_scope ON items(scope);
                CREATE TABLE IF NOT EXISTS proposals (
                    proposal_id TEXT PRIMARY KEY, scope TEXT NOT NULL,
                    payload TEXT NOT NULL, decision TEXT NOT NULL DEFAULT ''
                );
            """)
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        finally:
            connection.close()

    def retrieve(self, query: MemoryQuery) -> MemoryResult:
        if not query.scopes or query.max_items <= 0:
            return MemoryResult()
        placeholders = ",".join("?" for _ in query.scopes)
        eligible = f"scope IN ({placeholders}) AND (? = '' OR run_id != ?)"
        parameters = (*query.scopes, query.exclude_run_id, query.exclude_run_id)
        with self._connect() as connection:
            payloads = _texts(connection.execute(
                f"SELECT payload FROM (SELECT payload FROM items WHERE {eligible} "
                "AND json_extract(payload, '$.kind') = 'experience' ORDER BY rowid DESC LIMIT 200) "
                f"UNION ALL SELECT payload FROM items WHERE {eligible} "
                "AND json_extract(payload, '$.kind') IN ('preference', 'goal', 'hypothesis')",
                (*parameters, *parameters),
            ))
        items: list[MemoryItem] = []
        for payload in payloads:
            self._host.check_cancelled()
            value: object = json.loads(payload)
            item = _item(value)
            if item.expires_at and datetime.fromisoformat(item.expires_at) <= datetime.fromisoformat(query.as_of):
                continue
            items.append(item)
        def rank(item: MemoryItem) -> tuple[float, str]:
            return -relevance(query.text, item, query.as_of), item.item_id

        ranked = sorted(items, key=rank)
        return MemoryResult(tuple(item for item in ranked
                                  if item.kind != "experience" or relevance(query.text, item, query.as_of) > 0)
                            [:query.max_items])

    def observe(self, event: MemoryEvent) -> None:
        if not event.event_id or not event.provenance or not event.scope:
            raise ValueError("memory events require identity, provenance and scope")
        with self._connect(write=True) as connection:
            cursor = connection.execute("INSERT OR IGNORE INTO events VALUES (?)", (event.event_id,))
            if cursor.rowcount == 0:
                return
            if event.kind == "task.completed":
                user = require_json_string(event.payload.get("user_text"), "user_text")
                assistant = require_json_string(event.payload.get("assistant_text"), "assistant_text")
                exchange: dict[str, str] = {"user": user, "assistant": assistant}
                text = json.dumps(exchange, ensure_ascii=False)
                item = MemoryItem(event.event_id, "experience", text, event.scope,
                                  event.provenance, event.occurred_at)
                connection.execute("INSERT INTO items VALUES (?, ?, ?, ?)",
                                   (item.item_id, item.scope, event.run_id, _payload(item)))
            elif event.kind == "memory.proposed":
                value = dict(event.payload)
                value["item_id"] = event.event_id
                value["scope"] = event.scope
                value["source_refs"] = list(event.provenance)
                value["created_at"] = event.occurred_at
                proposed = _item(value)
                if proposed.kind == "experience" or not proposed.text.strip():
                    raise ValueError("only explicit preference, goal or hypothesis proposals are reviewable")
                connection.execute("INSERT INTO proposals (proposal_id, scope, payload) VALUES (?, ?, ?)",
                                   (event.event_id, event.scope, _payload(proposed)))

    def proposals(self, scope: str) -> tuple[MemoryProposal, ...]:
        with self._connect() as connection:
            payloads = _texts(connection.execute(
                "SELECT payload FROM proposals WHERE scope = ? AND decision = '' ORDER BY rowid LIMIT 100", (scope,),
            ))
        result: list[MemoryProposal] = []
        for payload in payloads:
            value: object = json.loads(payload)
            item = _item(value)
            result.append(MemoryProposal(item.item_id, item, hashlib.sha256(payload.encode()).hexdigest()))
        return tuple(result)

    def decide(self, decision: MemoryDecision) -> None:
        if not decision.source_ref:
            raise ValueError("a memory decision requires a committed user decision reference")
        with self._connect(write=True) as connection:
            payloads = _texts(connection.execute(
                "SELECT payload FROM proposals WHERE proposal_id = ? AND decision = ''", (decision.proposal_id,),
            ))
            if not payloads or hashlib.sha256(payloads[0].encode()).hexdigest() != decision.version:
                raise ValueError("proposal is missing, changed or already decided")
            if decision.accepted:
                value: object = json.loads(payloads[0])
                item = _item(value)
                item = replace(item, source_refs=(*item.source_refs, decision.source_ref))
                connection.execute("INSERT INTO items VALUES (?, ?, '', ?)", (item.item_id, item.scope, _payload(item)))
            connection.execute("UPDATE proposals SET decision = ? WHERE proposal_id = ?",
                               (decision.source_ref, decision.proposal_id))

    def delete_scope(self, request: DeleteScopeRequest) -> DeleteResult:
        with self._connect(write=True) as connection:
            deleted = connection.execute("DELETE FROM items WHERE scope = ?", (request.scope,)).rowcount
            connection.execute("DELETE FROM proposals WHERE scope = ?", (request.scope,))
            if request.source_refs:
                for table in ("items", "proposals"):
                    payloads = _texts(connection.execute(f"SELECT payload FROM {table}"))
                    for payload in payloads:
                        value: object = json.loads(payload)
                        item = _item(value)
                        if set(item.source_refs).intersection(request.source_refs):
                            key = "item_id" if table == "items" else "proposal_id"
                            count = connection.execute(f"DELETE FROM {table} WHERE {key} = ?", (item.item_id,)).rowcount
                            if table == "items":
                                deleted += count
        return DeleteResult(deleted)

    def export_state(self) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {"enabled": True}
        with self._connect() as connection:
            for table in ("items", "proposals"):
                payloads = _texts(connection.execute(f"SELECT payload FROM {table} ORDER BY rowid"))
                decoded: list[JsonValue] = []
                for payload in payloads:
                    value: object = json.loads(payload)
                    decoded.append(decode_json_object(value))
                result[table] = decoded
        return result

    def maintain(self, *, as_of: str) -> None:
        now = datetime.fromisoformat(as_of)
        with self._connect(write=True) as connection:
            payloads = _texts(connection.execute("SELECT payload FROM items ORDER BY rowid"))
            for payload in payloads:
                self._host.check_cancelled()
                value: object = json.loads(payload)
                item = _item(value)
                if item.expires_at and datetime.fromisoformat(item.expires_at) <= now:
                    connection.execute("DELETE FROM items WHERE item_id = ?", (item.item_id,))
