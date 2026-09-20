"""Append-only runtime session log.

The log records bounded facts and references about runtime operations.  It does
not persist raw prompts, model replies, command output, or diffs.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codey.storage.atomic_io import write_bytes_atomic
from codey.storage.file_lock import with_file_lock
from codey.storage.local_store import DEFAULT_STATE_HOME, session_key
from codey.runtime.log.entries import (  # noqa: F401  # re-exported for compatibility
    RuntimeLogCorruption,
    RuntimeLogEntry,
    RuntimeLogError,
    RuntimeLogWriteError,
)

DEFAULT_MAX_ENTRY_BYTES = 64 * 1024
DEFAULT_MAX_LOG_BYTES = 4 * 1024 * 1024



@dataclass(frozen=True)
class _FileStamp:
    file_size: int
    mtime_ns: int


@dataclass(frozen=True)
class _ProjectionCache:
    stamp: _FileStamp
    entries: tuple[RuntimeLogEntry, ...]
    projection: Any


class RuntimeSessionLog:
    def __init__(
        self,
        state_home: str | Path | None = None,
        *,
        max_entry_bytes: int = DEFAULT_MAX_ENTRY_BYTES,
        max_log_bytes: int = DEFAULT_MAX_LOG_BYTES,
    ) -> None:
        self.state_home = Path(state_home) if state_home is not None else DEFAULT_STATE_HOME
        self.max_entry_bytes = max_entry_bytes
        self.max_log_bytes = max_log_bytes
        self._projection_cache: dict[str, _ProjectionCache] = {}

    def path_for(self, session_id: str) -> Path:
        return self.state_home / "runtime" / "sessions" / f"{session_key(session_id)}.jsonl"

    def read(self, session_id: str) -> tuple[RuntimeLogEntry, ...]:
        path = self.path_for(session_id)
        with with_file_lock(path):
            return self._read_unlocked(session_id, repair_tail=False)

    def projection(self, session_id: str):
        """Return the cached projection for a session.

        The returned projection is the process-cache object; callers must treat
        it as read-only and derive new state through reducer.apply_entries().
        """
        path = self.path_for(session_id)
        with with_file_lock(path):
            return self._cache_for_session_unlocked(session_id).projection

    def entries(self, session_id: str) -> tuple[RuntimeLogEntry, ...]:
        """Return cached valid entries, repairing a torn tail before caching."""
        path = self.path_for(session_id)
        with with_file_lock(path):
            return self._cache_for_session_unlocked(session_id).entries

    def _cache_for_session_unlocked(self, session_id: str) -> _ProjectionCache:
        path = self.path_for(session_id)
        current_stamp = _file_stamp(path)
        cached = self._projection_cache.get(session_id)
        if cached is not None and cached.stamp == current_stamp:
            return cached

        entries = self._read_unlocked(session_id, repair_tail=True)
        from codey.runtime.log.session_projection import reduce_session

        cache = _ProjectionCache(
            stamp=_file_stamp(path),
            entries=entries,
            projection=reduce_session(entries),
        )
        self._projection_cache[session_id] = cache
        return cache

    def delete_session(self, session_id: str) -> None:
        path = self.path_for(session_id)
        with with_file_lock(path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise RuntimeLogWriteError("unable to delete runtime log") from exc
            finally:
                self._projection_cache.pop(session_id, None)

    def _read_unlocked(
        self,
        session_id: str,
        *,
        repair_tail: bool,
    ) -> tuple[RuntimeLogEntry, ...]:
        path = self.path_for(session_id)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ()
        except OSError as exc:
            raise RuntimeLogCorruption("unable to read runtime log") from exc
        lines = [
            (line_no, line)
            for line_no, line in enumerate(raw.splitlines(), start=1)
            if line.strip()
        ]
        entries: list[RuntimeLogEntry] = []
        bad_tail = False
        for index, (line_no, line) in enumerate(lines):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                if index == len(lines) - 1:
                    bad_tail = True
                    break
                raise RuntimeLogCorruption(f"invalid runtime log JSON at line {line_no}") from exc
            entry = RuntimeLogEntry.from_payload(payload)
            if entry.session_id != session_id:
                raise RuntimeLogCorruption("runtime log entry session mismatch")
            entries.append(entry)
        from codey.runtime.log.compaction import _complete_batch_prefix

        valid = _complete_batch_prefix(entries)
        if repair_tail and (bad_tail or len(valid) < len(entries)):
            write_bytes_atomic(path, b"".join(entry.to_json_line().encode("utf-8") for entry in valid))
            self._projection_cache.pop(session_id, None)
        return tuple(valid)

    def mutate(
        self,
        session_id: str,
        mutation: Any,
    ) -> tuple[RuntimeLogEntry, ...]:
        """Run one read/decide/commit pass under the session mutation line.

        The callback receives the current process-local projection and valid
        entries while the log lock is held. It must return the bounded entries
        to commit and must not perform external effects.
        """
        batch_id = f"batch-{uuid.uuid4().hex}"
        path = self.path_for(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with with_file_lock(path):
            from codey.runtime.log.session_projection import apply_entries, reduce_session

            current_stamp = _file_stamp(path)
            current_size = current_stamp.file_size
            cached = self._projection_cache.get(session_id)
            if cached is not None and cached.stamp == current_stamp:
                base_entries = cached.entries
                base_projection = cached.projection
            else:
                base_entries = self._read_unlocked(session_id, repair_tail=True)
                current_stamp = _file_stamp(path)
                current_size = current_stamp.file_size

                base_projection = reduce_session(base_entries)

            incoming = tuple(mutation(base_projection, base_entries))
            if not incoming:
                return ()
            batch_count = len(incoming)
            rows = tuple(
                RuntimeLogEntry(
                    session_id=session_id,
                    lane=entry.get("lane"),
                    operation_id=entry.get("operation_id"),
                    kind=entry.get("kind"),
                    payload=entry.get("payload"),
                    batch_id=batch_id,
                    batch_index=index,
                    batch_count=batch_count,
                )
                for index, entry in enumerate(incoming)
            )
            encoded_rows = tuple(row.to_json_line().encode("utf-8") for row in rows)
            if any(len(encoded) > self.max_entry_bytes for encoded in encoded_rows):
                raise RuntimeLogWriteError("runtime log entry exceeds size limit")
            candidate_projection = apply_entries(base_projection, rows)
            total_new_bytes = sum(len(encoded) for encoded in encoded_rows)
            if current_size + total_new_bytes > max(0, self.max_log_bytes // 2):
                from codey.runtime.log.compaction import _compact_entries

                candidate = (*base_entries, *rows)
                compacted = _compact_entries(candidate)
                compacted_projection = reduce_session(compacted)
                encoded_compacted = tuple(
                    row.to_json_line().encode("utf-8") for row in compacted
                )
                if any(len(encoded) > self.max_entry_bytes for encoded in encoded_compacted):
                    raise RuntimeLogWriteError("runtime log entry exceeds size limit")
                if sum(len(encoded) for encoded in encoded_compacted) > self.max_log_bytes:
                    raise RuntimeLogWriteError("runtime log exceeds size limit")
                write_bytes_atomic(path, b"".join(encoded_compacted))
                self._projection_cache[session_id] = _ProjectionCache(
                    stamp=_file_stamp(path),
                    entries=compacted,
                    projection=compacted_projection,
                )
                return rows
            from codey.storage.atomic_io import append_bytes_durable

            append_bytes_durable(path, list(encoded_rows))
            self._projection_cache[session_id] = _ProjectionCache(
                stamp=_file_stamp(path),
                entries=(*base_entries, *rows),
                projection=candidate_projection,
            )
        return rows


def _file_stamp(path: Path) -> _FileStamp:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return _FileStamp(file_size=0, mtime_ns=0)
    return _FileStamp(file_size=stat.st_size, mtime_ns=stat.st_mtime_ns)
