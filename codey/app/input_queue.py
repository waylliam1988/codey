"""Durable conversation inputs, with an admission fence before submission.

The queue lock never covers application callbacks. A dispatch whose outcome is
unknown is retained for status checking, never automatically submitted again.
"""
from __future__ import annotations

import builtins
import copy
import hashlib
import json
import logging
import re
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from codey.storage.conversation_db import conversation_database, initialize_conversation_database

Submit = Callable[[dict[str, Any]], tuple[int, dict[str, Any]]]
logger = logging.getLogger(__name__)


def _submission_state(status: int, result: dict[str, Any]) -> str:
    if status == 200 and result.get("ok") is True:
        return "submitted"
    if result.get("error") == "stopped":
        return "paused"
    if status in {409, 503} and result.get("error") in {"busy", "browser worker busy"}:
        return "queued"
    if status >= 500 or 200 <= status < 300:
        return "unconfirmed"
    return "failed"


class ConversationInputQueue:
    def __init__(self, root: Path, *, emit: Callable[[dict[str, Any]], None]) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "conversation_inputs.sqlite3"
        self.emit = emit
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.closed = threading.Event()
        self.worker: threading.Thread | None = None
        initialize_conversation_database(self.path, "CREATE TABLE IF NOT EXISTS inputs (id TEXT PRIMARY KEY, run_id TEXT UNIQUE, state TEXT, revision INTEGER, payload TEXT NOT NULL)")
        with conversation_database(self.path) as database:
            rows = database.execute("SELECT id, revision, payload FROM inputs WHERE state NOT IN ('completed', 'cancelled') OR rowid IN (SELECT rowid FROM inputs WHERE state IN ('completed', 'cancelled') ORDER BY rowid DESC LIMIT 128) ORDER BY rowid").fetchall()
        self.items: dict[str, dict[str, Any]] = {identity: json.loads(payload) for identity, _revision, payload in rows}
        self.saved_revisions = {identity: revision for identity, revision, _payload in rows}
        recovering = False
        for item in self.items.values():
            if item["state"] in {"dispatching", "submitted"}:
                item.update(state="unconfirmed", revision=item["revision"] + 1)
                recovering = True
        if recovering:
            self._save()

    def _save(self) -> None:
        dirty = [item for identity, item in self.items.items() if self.saved_revisions.get(identity) != item["revision"]]
        with conversation_database(self.path) as database:
            database.executemany("INSERT INTO inputs (id,run_id,state,revision,payload) VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state,revision=excluded.revision,payload=excluded.payload", [
                (item["id"], item["request"]["run_id"], item["state"], item["revision"], json.dumps(item, ensure_ascii=False)) for item in dirty
            ])
        self.saved_revisions.update({item["id"]: item["revision"] for item in dirty})
        old = [identity for identity, item in self.items.items() if item["state"] in {"completed", "cancelled"}][:-128]
        for identity in old:
            self.items.pop(identity)
            self.saved_revisions.pop(identity, None)

    def _stored(self, identity: str) -> dict[str, Any] | None:
        with conversation_database(self.path) as database:
            row = database.execute("SELECT payload FROM inputs WHERE id=?", (identity,)).fetchone()
        return json.loads(row[0]) if row is not None else None

    def get(self, identity: str) -> dict[str, Any] | None:
        with self.lock:
            return copy.deepcopy(self.items.get(identity) or self._stored(identity))

    def list(self, session_id: str = "") -> builtins.list[dict[str, Any]]:
        with self.lock:
            return copy.deepcopy([item for item in self.items.values()
                if not session_id or item["request"]["session_id"] == session_id])

    def _publish(self, item: dict[str, Any]) -> None:
        self.emit({"type": "conversation_input", "session_id": item["request"]["session_id"], "input": item})
        self.wake.set()

    def create(self, identity: str, request: dict[str, Any]) -> dict[str, Any]:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", identity) or not request.get("session_id") or not request.get("run_id"):
            raise ValueError("invalid input identity")
        if not isinstance(request.get("task"), str) or not 0 < len(request["task"].strip()) <= 64_000:
            raise ValueError("invalid input text")
        digest = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        with self.lock:
            old = self.items.get(identity) or self._stored(identity)
            if old:
                if old["origin"] != digest:
                    raise ValueError("input identity conflict")
                return copy.deepcopy(old)
            with conversation_database(self.path) as database:
                if database.execute("SELECT 1 FROM inputs WHERE run_id=?", (request["run_id"],)).fetchone():
                    raise ValueError("run identity conflict")
            if sum(item["state"] not in {"completed", "cancelled"} for item in self.items.values()) >= 64:
                raise ValueError("input queue is full")
            item = {"schema_version": 1, "id": identity, "request": copy.deepcopy(request), "state": "queued", "revision": 1, "origin": digest}
            self.items[identity] = item
            try:
                self._save()
            except Exception:
                self.items.pop(identity)
                raise
            snapshot = copy.deepcopy(item)
        self._publish(snapshot)
        return snapshot

    def _change(self, identity: str, revision: int, state: str, text: str | None = None) -> dict[str, Any]:
        if text is not None and not 0 < len(text.strip()) <= 64_000:
            raise ValueError("invalid input text")
        with self.lock:
            item = self.items.get(identity)
            if not item or item["revision"] != revision:
                raise ValueError("input revision conflict")
            if item["state"] not in {"queued", "paused", "failed"}:
                raise ValueError("input is already being delivered")
            previous = copy.deepcopy(item)
            item.update(state=state, revision=revision + 1)
            if text is not None:
                item["request"]["task"] = text
            try:
                self._save()
            except Exception:
                self.items[identity] = previous
                raise
            snapshot = copy.deepcopy(item)
        self._publish(snapshot)
        return snapshot

    def edit(self, identity: str, revision: int, text: str) -> dict[str, Any]:
        return self._change(identity, revision, "queued", text)

    def remove(self, identity: str, revision: int) -> dict[str, Any]:
        return self._change(identity, revision, "cancelled")

    def resume(self, identity: str, revision: int) -> dict[str, Any]:
        return self._change(identity, revision, "queued")

    def pause(self, session_id: str = "") -> None:
        snapshots = []
        with self.lock:
            for item in self.items.values():
                if item["state"] == "queued" and (not session_id or item["request"].get("session_id") == session_id):
                    item.update(state="paused", revision=item["revision"] + 1)
                    snapshots.append(copy.deepcopy(item))
            if snapshots:
                self._save()
        for item in snapshots:
            self._publish(item)

    def completed(self, run_id: str, terminal: dict[str, Any]) -> None:
        snapshot = None
        with self.lock:
            for item in self.items.values():
                if item["request"]["run_id"] == run_id and item["state"] in {"dispatching", "submitted", "unconfirmed"}:
                    item.update(state="completed", revision=item["revision"] + 1, terminal=copy.deepcopy(terminal))
                    self._save()
                    snapshot = copy.deepcopy(item)
                    break
        if snapshot:
            self._publish(snapshot)

    def dispatch_one(self, ready: Callable[[], bool], submit: Submit) -> bool:
        if self.closed.is_set() or not ready():
            return False
        with self.lock:
            item = next((item for item in self.items.values() if item["state"] == "queued"), None)
            if item is None:
                return False
            previous = copy.deepcopy(item)
            item.update(state="dispatching", revision=item["revision"] + 1)
            try:
                self._save()
            except Exception:
                item.update(previous)
                raise
            snapshot = copy.deepcopy(item)
        self._publish(snapshot)
        try:
            status, result = submit(copy.deepcopy(snapshot["request"]))
            state = _submission_state(status, result)
            error = "" if state == "submitted" else str(result.get("error") or "Input was not accepted")
        except Exception as exc:
            state, error = "unconfirmed", str(exc)[:500]
        with self.lock:
            item = self.items.get(snapshot["id"]) or self._stored(snapshot["id"])
            if item is None:
                raise RuntimeError("durable input receipt disappeared")
            if item["state"] == "dispatching":
                item.update(state=state, error=error, revision=item["revision"] + 1)
                self._save()
            snapshot = copy.deepcopy(item)
        self._publish(snapshot)
        return True

    def bind(self, ready: Callable[[], bool], submit: Submit) -> None:
        with self.lock:
            if self.worker is not None or self.closed.is_set():
                return
            def work() -> None:
                while not self.closed.is_set():
                    self.wake.clear()
                    try:
                        self.dispatch_one(ready, submit)
                    except Exception:
                        logger.exception("queued conversation input could not be dispatched")
                    self.wake.wait(0.5)
            self.worker = threading.Thread(target=work, name="conversation-inputs", daemon=True)
            self.worker.start()

    def close(self) -> bool:
        self.closed.set()
        self.wake.set()
        if self.worker is not None:
            self.worker.join(timeout=2)
            if self.worker.is_alive():
                return False
        return True
