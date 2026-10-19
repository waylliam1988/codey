"""Durable conversation inputs, with an admission fence before submission.

The queue lock never covers application callbacks. A dispatch whose outcome is
unknown is retained for status checking, never automatically submitted again.
"""
from __future__ import annotations

import builtins
import contextlib
import copy
import hashlib
import json
import logging
import re
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

from codey.storage.conversation_db import conversation_database, initialize_conversation_database

Submit = Callable[[dict[str, Any]], tuple[int, dict[str, Any]]]
logger = logging.getLogger(__name__)


def _jsonable(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


class DurableConversationInputQueue:
    """Projection and dispatch facade backed only by ``DurableSession``.

    The browser still speaks the historical input-queue shape, but admission
    is a durable submission/task commit.  The old SQLite queue remains
    available as a standalone compatibility test fixture; AppContext uses
    this class exclusively.
    """

    def __init__(self, durable: Any, *, emit: Callable[[dict[str, Any]], None]) -> None:
        self.durable = durable
        self.emit = emit
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.closed = threading.Event()
        self.worker: threading.Thread | None = None

    @staticmethod
    def _state_for_task(task: Any) -> str:
        raw = task.state.get("input_state") if isinstance(task.state, dict) else None
        if raw in {"paused", "cancelled"}:
            return str(raw)
        status = str(task.status)
        if status == "completed" or task.phase == "completed":
            return "completed"
        if status == "cancelled" or task.phase == "cancelled":
            return "cancelled"
        if status == "failed" or task.phase == "failed":
            return "unconfirmed"
        if status == "running" or task.phase != "accepted":
            return "submitted"
        return "queued"

    @classmethod
    def _item_for_task(cls, task: Any) -> dict[str, Any] | None:
        state = task.state
        if not isinstance(state, Mapping):
            return None
        identity = state.get("input_identity")
        request = state.get("input_request")
        if not isinstance(identity, str) or not identity or not isinstance(request, Mapping):
            return None
        item_state = cls._state_for_task(task)
        normalized = _jsonable(dict(request))
        if not isinstance(normalized, dict):
            return None
        request = cast(dict[str, Any], normalized)
        return {
            "schema_version": 1,
            "id": identity,
            "request": copy.deepcopy(request),
            "state": item_state,
            "revision": int(task.revision),
            "origin": hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest(),
        }

    def _items(self, session_id: str = "") -> builtins.list[dict[str, Any]]:
        items: builtins.list[dict[str, Any]] = []
        for task in self.durable.tasks():
            item = self._item_for_task(task)
            if item is None:
                continue
            if session_id and item["request"].get("session_id") != session_id:
                continue
            items.append(item)
        return items

    def get(self, identity: str) -> dict[str, Any] | None:
        with self.lock:
            for item in self._items():
                if item["id"] == identity:
                    return item
            return None

    def list(self, session_id: str = "") -> builtins.list[dict[str, Any]]:
        with self.lock:
            return self._items(session_id)

    def create(self, identity: str, request: dict[str, Any]) -> dict[str, Any]:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", identity):
            raise ValueError("invalid input identity")
        if not request.get("session_id") or not request.get("run_id"):
            raise ValueError("invalid input identity")
        if not isinstance(request.get("task"), str) or not 0 < len(request["task"].strip()) <= 64_000:
            raise ValueError("invalid input text")
        # Admission freezes the same provider contract as the direct /api/run
        # path.  The browser sends a compact model selection; a resumed task
        # must carry the complete, validated selection instead of trying to
        # reconstruct it from current settings.
        from codey.providers.snapshot import capture_provider_selection, provider_snapshot

        provider_id = str(request.get("provider") or "")
        raw_selection = request.get("model_selection")
        selection = (
            capture_provider_selection(provider_id, raw_selection)
            if raw_selection is not None
            else None
        )
        admitted_request = copy.deepcopy(request)
        if selection is not None:
            admitted_request["model_selection"] = selection.to_payload()
        with self.lock:
            existing = self.get(identity)
            if existing is not None:
                if existing["origin"] != hashlib.sha256(json.dumps(admitted_request, sort_keys=True).encode()).hexdigest():
                    raise ValueError("input identity conflict")
                return existing
            snapshot = provider_snapshot(provider_id, selection)
            self.durable.admit_input(
                conversation_id=str(request["session_id"]),
                request_id=identity,
                task_id=str(request["run_id"]),
                text=str(request["task"]),
                provider_snapshot=snapshot,
                task_state={
                    "input_identity": identity,
                    "input_request": admitted_request,
                    "input_state": "queued",
                    **admitted_request,
                },
            )
            item = self.get(identity)
        if item is None:
            raise RuntimeError("durable input admission did not produce a projection")
        self.emit({"type": "conversation_input", "session_id": request["session_id"], "input": item})
        self.wake.set()
        return item

    def dispatch_one(self, ready: Callable[[], bool], submit: Submit) -> bool:
        if self.closed.is_set() or not ready():
            return False
        with self.lock:
            item = next((item for item in self._items() if item["state"] == "queued"), None)
            if item is None:
                return False
            task_id = str(item["request"]["run_id"])
            task = self.durable.task(task_id)
            if str(task.status) == "pending":
                self.durable.claim_task(task_id)
            elif str(task.status) != "running":
                return False
            item = self.get(str(item["id"])) or item
        try:
            status, result = submit(copy.deepcopy(item["request"]))
        except Exception as exc:
            status, result = 500, {"error": str(exc)[:500]}
        if status < 200 or status >= 300 or result.get("ok") is not True:
            with self.lock, contextlib.suppress(Exception):
                self.durable.unclaim_task(task_id)
        self.emit({"type": "conversation_input", "session_id": item["request"]["session_id"], "input": self.get(str(item["id"])) or item})
        return True

    def bind(self, ready: Callable[[], bool], submit: Submit) -> None:
        # DurableTaskScheduler owns admission dispatch. Keeping a second
        # worker here would create a duplicate execution race.
        return None

    def edit(self, identity: str, revision: int, text: str) -> dict[str, Any]:
        raise ValueError("durable input entries are immutable; submit a new request")

    def remove(self, identity: str, revision: int) -> dict[str, Any]:
        raise ValueError("durable input cancellation is handled by task control")

    def resume(self, identity: str, revision: int) -> dict[str, Any]:
        raise ValueError("durable input resumption is handled by task scheduler")

    def pause(self, session_id: str = "") -> None:
        self.wake.set()

    def completed(self, run_id: str, terminal: dict[str, Any]) -> None:
        self.wake.set()

    def close(self) -> bool:
        self.closed.set()
        self.wake.set()
        return True


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
