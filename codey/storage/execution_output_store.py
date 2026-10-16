"""Bounded, restart-readable execution output snapshots."""
from __future__ import annotations

import codecs
import queue
import sqlite3
import threading
from contextlib import suppress
from pathlib import Path
from typing import Any

from codey.storage.conversation_db import conversation_database

SCHEMA = """
CREATE TABLE IF NOT EXISTS execution_outputs (
  execution_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  state TEXT NOT NULL,
  revision INTEGER NOT NULL,
  stdout TEXT NOT NULL,
  stderr TEXT NOT NULL,
  stdout_bytes INTEGER NOT NULL,
  stderr_bytes INTEGER NOT NULL,
  exit_code INTEGER,
  max_bytes INTEGER NOT NULL
)
"""


class ExecutionOutputStore:
    def __init__(self, root: str | Path, *, max_bytes: int = 256 * 1024) -> None:
        self.path = Path(root) / "execution_outputs.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max(1, int(max_bytes))
        with conversation_database(self.path) as database:
            database.execute("PRAGMA journal_mode=WAL")
            database.executescript(SCHEMA)

    def close(self) -> None:
        """Checkpoint and remove the persistent WAL handles before teardown."""
        if not self.path.exists():
            return
        with conversation_database(self.path) as database:
            database.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            database.execute("PRAGMA journal_mode=DELETE")

    def create(self, execution_id: str, *, session_id: str, run_id: str, max_bytes: int | None = None) -> None:
        if not execution_id or not session_id or not run_id:
            raise ValueError("execution identity required")
        with conversation_database(self.path) as database:
            database.execute("INSERT OR IGNORE INTO execution_outputs(execution_id,session_id,run_id,state,revision,stdout,stderr,stdout_bytes,stderr_bytes,exit_code,max_bytes) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (execution_id, session_id, run_id, "running", 0, "", "", 0, 0, None, max(1, int(max_bytes or self.max_bytes))))

    def append(self, execution_id: str, channel: str, chunk: bytes) -> dict[str, Any]:
        if channel not in {"stdout", "stderr"}:
            raise ValueError("invalid output channel")
        with conversation_database(self.path) as database:
            row = database.execute("SELECT stdout,stderr,stdout_bytes,stderr_bytes,max_bytes,revision,state,exit_code FROM execution_outputs WHERE execution_id=?", (execution_id,)).fetchone()
            if row is None:
                raise KeyError(execution_id)
            text = chunk.decode("utf-8", errors="replace")
            return self._append_text_locked(database, execution_id, channel, text, row)

    def append_text(self, execution_id: str, channel: str, text: str) -> dict[str, Any]:
        if channel not in {"stdout", "stderr"}:
            raise ValueError("invalid output channel")
        with conversation_database(self.path) as database:
            row = database.execute("SELECT stdout,stderr,stdout_bytes,stderr_bytes,max_bytes,revision,state,exit_code FROM execution_outputs WHERE execution_id=?", (execution_id,)).fetchone()
            if row is None:
                raise KeyError(execution_id)
            return self._append_text_locked(database, execution_id, channel, str(text), row)

    def _append_text_locked(self, database: sqlite3.Connection, execution_id: str, channel: str, text: str, row: Any) -> dict[str, Any]:
            stdout, stderr = str(row[0]), str(row[1])
            stdout_bytes, stderr_bytes, max_bytes, revision = int(row[2]), int(row[3]), int(row[4]), int(row[5])
            if channel == "stdout":
                stdout, stdout_bytes = _bounded_text(stdout, stdout_bytes, text, max_bytes)
            else:
                stderr, stderr_bytes = _bounded_text(stderr, stderr_bytes, text, max_bytes)
            revision += 1
            database.execute("UPDATE execution_outputs SET stdout=?,stderr=?,stdout_bytes=?,stderr_bytes=?,revision=? WHERE execution_id=?", (stdout, stderr, stdout_bytes, stderr_bytes, revision, execution_id))
            return self.snapshot(execution_id, database=database)

    def settled(self, execution_id: str, exit_code: int | None) -> dict[str, Any]:
        with conversation_database(self.path) as database:
            database.execute("UPDATE execution_outputs SET state='settled',revision=revision+1,exit_code=? WHERE execution_id=?", (exit_code if type(exit_code) is int else None, execution_id))
            return self.snapshot(execution_id, database=database)

    def unavailable(self, execution_id: str) -> None:
        with conversation_database(self.path) as database:
            database.execute("UPDATE execution_outputs SET state='unknown',revision=revision+1 WHERE execution_id=?", (execution_id,))

    def snapshot(self, execution_id: str, *, database: sqlite3.Connection | None = None) -> dict[str, Any]:
        if database is None:
            with conversation_database(self.path) as connection:
                return self.snapshot(execution_id, database=connection)
        row = database.execute("SELECT execution_id,session_id,run_id,state,revision,stdout,stderr,stdout_bytes,stderr_bytes,exit_code,max_bytes FROM execution_outputs WHERE execution_id=?", (execution_id,)).fetchone()
        if row is None:
            raise KeyError(execution_id)
        return {"schema_version": 1, "execution_id": row[0], "session_id": row[1], "run_id": row[2], "execution_state": row[3], "revision": row[4], "stdout": _stream_payload(row[5], row[7], row[10]), "stderr": _stream_payload(row[6], row[8], row[10]), "exit_code": row[9], "result_revision": row[4] if row[3] == "settled" else None}


def _bounded_text(existing: str, total: int, added: str, limit: int) -> tuple[str, int]:
    total += len(added.encode("utf-8"))
    text = existing + added
    if len(text.encode("utf-8")) <= limit:
        return text, total
    head = max(1, limit // 2)
    encoded = text.encode("utf-8")
    visible = encoded[:head].decode("utf-8", errors="ignore") + encoded[-(limit - head):].decode("utf-8", errors="ignore")
    return visible, total


def _stream_payload(text: str, total: int, limit: int) -> dict[str, Any]:
    stored = len(text.encode("utf-8"))
    return {"text": text, "head": text[: max(1, len(text) // 2)], "tail": text[max(1, len(text) // 2):], "total_bytes": total, "omitted_bytes": max(0, total - stored), "truncated": total > stored}


class ExecutionOutputObserver:
    def __init__(self, store: ExecutionOutputStore, execution_id: str, on_update: Any = None) -> None:
        self.store = store
        self.execution_id = execution_id
        self.on_update = on_update
        self._queue: queue.Queue[tuple[str, bytes] | None] = queue.Queue(maxsize=128)
        self._closed = threading.Event()
        self._close_lock = threading.Lock()
        self._decoders = {
            "stdout": codecs.getincrementaldecoder("utf-8")(errors="replace"),
            "stderr": codecs.getincrementaldecoder("utf-8")(errors="replace"),
        }
        self._worker = threading.Thread(target=self._drain, name=f"execution-output-{execution_id}", daemon=True)
        self._worker.start()

    def offer(self, channel: str, chunk: bytes) -> None:
        if self._closed.is_set() or channel not in self._decoders:
            return
        # Decode only in the observer worker so a partial UTF-8 sequence is
        # carried across pipe reads instead of being replaced prematurely.
        try:
            self._queue.put_nowait((channel, bytes(chunk)))
        except queue.Full:
            # Replace one pending snapshot with the newest bounded chunk. The
            # final process capture remains authoritative if this observer is
            # unavailable.
            try:
                self._queue.get_nowait()
                self._queue.put_nowait((channel, bytes(chunk)))
            except queue.Empty:
                pass

    def _drain(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                for channel, decoder in self._decoders.items():
                    with suppress(Exception):
                        tail = decoder.decode(b"", final=True)
                        if tail:
                            self.store.append_text(self.execution_id, channel, tail)
                self._closed.set()
                return
            with suppress(Exception):
                text = self._decoders[item[0]].decode(item[1], final=False)
                if text:
                    snapshot = self.store.append_text(self.execution_id, item[0], text)
                    if callable(self.on_update):
                        with suppress(Exception):
                            self.on_update(snapshot)

    def close(self) -> None:
        with self._close_lock:
            if self._closed.is_set():
                return
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(None)
            except queue.Empty:
                return
        self._worker.join()
