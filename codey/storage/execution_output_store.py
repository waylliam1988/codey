"""Bounded, restart-readable execution output snapshots."""
from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Sequence
from contextlib import suppress
from pathlib import Path
from typing import TypedDict

from codey.runtime.core.output_capture import BoundedByteCapture, CapturedText
from codey.storage.conversation_db import conversation_database
from codey.types.json_value import decode_json_object, require_json_boolean, require_json_integer, require_json_string

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


class StreamSnapshot(TypedDict):
    text: str
    head: str
    tail: str
    total_bytes: int
    omitted_bytes: int
    truncated: bool
    encoding: str
    decoding_errors: bool


class ExecutionSnapshot(TypedDict):
    schema_version: int
    execution_id: str
    session_id: str
    run_id: str
    execution_state: str
    revision: int
    stdout: StreamSnapshot
    stderr: StreamSnapshot
    exit_code: int | None
    result_revision: int | None


class ExecutionOutputStore:
    def __init__(self, root: str | Path, *, max_bytes: int = 256 * 1024) -> None:
        self.path = Path(root) / "execution_outputs.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max(1, int(max_bytes))
        with conversation_database(self.path) as database:
            database.execute("PRAGMA journal_mode=WAL")
            database.executescript(SCHEMA)
            columns = {row[1] for row in database.execute("PRAGMA table_info(execution_outputs)")}
            for column in ("stdout_meta", "stderr_meta"):
                if column not in columns:
                    database.execute(f"ALTER TABLE execution_outputs ADD COLUMN {column} TEXT NOT NULL DEFAULT '{{}}'")

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

    def delete_session(self, session_id: str) -> None:
        with conversation_database(self.path) as database:
            database.execute("DELETE FROM execution_outputs WHERE session_id=?", (session_id,))

    def append(self, execution_id: str, channel: str, chunk: bytes) -> ExecutionSnapshot:
        if channel not in {"stdout", "stderr"}:
            raise ValueError("invalid output channel")
        with conversation_database(self.path) as database:
            row = database.execute("SELECT stdout,stderr,stdout_bytes,stderr_bytes,max_bytes,revision,state,exit_code FROM execution_outputs WHERE execution_id=?", (execution_id,)).fetchone()
            if row is None:
                raise KeyError(execution_id)
            capture = BoundedByteCapture(head_limit=len(chunk), tail_limit=0)
            capture.feed(chunk)
            text = capture.finish().text
            return self._append_text_locked(database, execution_id, channel, text, row)

    def append_text(self, execution_id: str, channel: str, text: str) -> ExecutionSnapshot:
        if channel not in {"stdout", "stderr"}:
            raise ValueError("invalid output channel")
        with conversation_database(self.path) as database:
            row = database.execute("SELECT stdout,stderr,stdout_bytes,stderr_bytes,max_bytes,revision,state,exit_code FROM execution_outputs WHERE execution_id=?", (execution_id,)).fetchone()
            if row is None:
                raise KeyError(execution_id)
            return self._append_text_locked(database, execution_id, channel, str(text), row)

    def _append_text_locked(self, database: sqlite3.Connection, execution_id: str, channel: str, text: str, row: Sequence[object]) -> ExecutionSnapshot:
        stdout, stderr = str(row[0]), str(row[1])
        stdout_bytes, stderr_bytes, max_bytes, revision = (require_json_integer(row[index], "output row") for index in (2, 3, 4, 5))
        if channel == "stdout":
            stdout, stdout_bytes = _bounded_text(stdout, stdout_bytes, text, max_bytes)
        else:
            stderr, stderr_bytes = _bounded_text(stderr, stderr_bytes, text, max_bytes)
        revision += 1
        database.execute(f"UPDATE execution_outputs SET stdout=?,stderr=?,stdout_bytes=?,stderr_bytes=?,revision=?,{channel}_meta='{{}}' WHERE execution_id=?", (stdout, stderr, stdout_bytes, stderr_bytes, revision, execution_id))
        return self.snapshot(execution_id, database=database)

    def capture_limit(self, execution_id: str) -> int:
        with conversation_database(self.path) as database:
            row = database.execute("SELECT max_bytes FROM execution_outputs WHERE execution_id=?", (execution_id,)).fetchone()
            if row is None:
                raise KeyError(execution_id)
            return require_json_integer(row[0], "max_bytes")

    def replace_capture(self, execution_id: str, channel: str, capture: CapturedText) -> ExecutionSnapshot:
        if channel not in {"stdout", "stderr"}:
            raise ValueError("invalid output channel")
        meta = json.dumps({"head": capture.head, "tail": capture.tail, "omitted_bytes": capture.omitted_bytes,
                           "encoding": capture.encoding, "decoding_errors": capture.decoding_errors})
        with conversation_database(self.path) as database:
            database.execute(f"UPDATE execution_outputs SET {channel}=?,{channel}_bytes=?,{channel}_meta=?,revision=revision+1 WHERE execution_id=?",
                             (capture.text, capture.total_bytes, meta, execution_id))
            return self.snapshot(execution_id, database=database)

    def settled(self, execution_id: str, exit_code: int | None) -> ExecutionSnapshot:
        with conversation_database(self.path) as database:
            database.execute("UPDATE execution_outputs SET state=CASE WHEN state='unknown' THEN state ELSE 'settled' END,revision=revision+1,exit_code=? WHERE execution_id=?", (exit_code, execution_id))
            return self.snapshot(execution_id, database=database)

    def unavailable(self, execution_id: str) -> None:
        with conversation_database(self.path) as database:
            database.execute("UPDATE execution_outputs SET state='unknown',revision=revision+1 WHERE execution_id=?", (execution_id,))

    def snapshot(self, execution_id: str, *, database: sqlite3.Connection | None = None) -> ExecutionSnapshot:
        if database is None:
            with conversation_database(self.path) as connection:
                return self.snapshot(execution_id, database=connection)
        return self._snapshot(execution_id, database)

    @classmethod
    def read_existing(cls, root: Path, execution_id: str) -> ExecutionSnapshot:
        path = root / "execution_outputs.sqlite3"
        if not path.is_file():
            raise KeyError(execution_id)
        with conversation_database(path) as database:
            return cls._snapshot(execution_id, database)

    @staticmethod
    def _snapshot(execution_id: str, database: sqlite3.Connection) -> ExecutionSnapshot:
        row = database.execute("SELECT execution_id,session_id,run_id,state,revision,stdout,stderr,stdout_bytes,stderr_bytes,exit_code,max_bytes,stdout_meta,stderr_meta FROM execution_outputs WHERE execution_id=?", (execution_id,)).fetchone()
        if row is None:
            raise KeyError(execution_id)
        revision = require_json_integer(row[4], "revision")
        return {"schema_version": 1, "execution_id": str(row[0]), "session_id": str(row[1]), "run_id": str(row[2]),
                "execution_state": str(row[3]), "revision": revision,
                "stdout": _stream_payload(str(row[5]), require_json_integer(row[7], "stdout_bytes"), str(row[11])),
                "stderr": _stream_payload(str(row[6]), require_json_integer(row[8], "stderr_bytes"), str(row[12])),
                "exit_code": None if row[9] is None else require_json_integer(row[9], "exit_code"),
                "result_revision": revision if row[3] == "settled" else None}


def _bounded_text(existing: str, total: int, added: str, limit: int) -> tuple[str, int]:
    total += len(added.encode("utf-8"))
    text = existing + added
    if len(text.encode("utf-8")) <= limit:
        return text, total
    head = limit // 4
    encoded = text.encode("utf-8")
    visible = encoded[:head].decode("utf-8", errors="ignore") + encoded[-(limit - head):].decode("utf-8", errors="ignore")
    return visible, total


def _stream_payload(text: str, total: int, encoded_meta: str) -> StreamSnapshot:
    meta = decode_json_object(json.loads(encoded_meta))
    omitted = require_json_integer(meta.get("omitted_bytes", max(0, total - len(text.encode("utf-8")))), "omitted_bytes")
    return {"text": text,
            "head": require_json_string(meta.get("head", text[:len(text) // 2]), "head"),
            "tail": require_json_string(meta.get("tail", text[len(text) // 2:]), "tail"),
            "total_bytes": total, "omitted_bytes": omitted, "truncated": omitted > 0,
            "encoding": require_json_string(meta.get("encoding", "utf-8"), "encoding"),
            "decoding_errors": require_json_boolean(meta.get("decoding_errors", False), "decoding_errors")}


class ExecutionOutputObserver:
    def __init__(self, store: ExecutionOutputStore, execution_id: str,
                 on_update: Callable[[ExecutionSnapshot], None] | None = None, *,
                 fallback_encoding: str | None = None) -> None:
        self.store = store
        self.execution_id = execution_id
        self.on_update = on_update
        limit = store.capture_limit(execution_id)
        self._captures = {channel: BoundedByteCapture(head_limit=limit // 4, tail_limit=limit - limit // 4,
                                                    fallback_encoding=fallback_encoding)
                          for channel in ("stdout", "stderr")}
        self._lock = threading.Lock()
        self._closing = False
        self._dirty: set[str] = set()
        self._wake = threading.Event()
        self._worker = threading.Thread(target=self._drain, name=f"execution-output-{execution_id}", daemon=True)
        self._worker.start()

    def offer(self, channel: str, chunk: bytes) -> None:
        with self._lock:
            if self._closing or channel not in self._captures:
                return
            # Coalesce notifications, never chunks. Slow persistence cannot
            # discard bytes or corrupt a multibyte decoder's state.
            self._captures[channel].feed(chunk)
            self._dirty.add(channel)
            self._wake.set()

    def _drain(self) -> None:
        while True:
            self._wake.wait()
            with self._lock:
                self._wake.clear()
                closing = self._closing
                channels = self._captures.keys() if closing else self._dirty
                snapshots = {channel: self._captures[channel].finish(final=closing) for channel in channels}
                self._dirty.clear()
            try:
                for channel, capture in snapshots.items():
                    snapshot = self.store.replace_capture(self.execution_id, channel, capture)
                    if self.on_update is not None:
                        with suppress(Exception):
                            self.on_update(snapshot)
            except (OSError, sqlite3.Error, KeyError, ValueError):
                with suppress(OSError, sqlite3.Error):
                    self.store.unavailable(self.execution_id)
            if closing:
                return

    def close(self) -> None:
        with self._lock:
            self._closing = True
            self._wake.set()
        self._worker.join()
