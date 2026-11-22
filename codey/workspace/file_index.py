"""Derived metadata snapshots. Filesystem contents and search receipts stay authoritative."""

from __future__ import annotations

import hashlib
import json
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import RLock
from uuid import uuid4

from codey.runtime.core import cancellation
from codey.storage.local_store import StoreCorruption, read_json_strict, write_json_atomic
from codey.types.json_value import (
    JsonValue,
    decode_json_object,
    require_json_boolean,
    require_json_integer,
    require_json_string,
)
from codey.workspace.bounded_scan import (
    BoundedScanBudget,
    DirectoryEntries,
    collect_bounded_dir_entries,
    iter_bounded_files,
)

_SNAPSHOT_SCHEMA = 3


@dataclass(frozen=True)
class FileIndexEntry:
    path: Path
    size: int
    mtime_ns: int
    mode: int


@dataclass(frozen=True)
class DiscoveryCoverage:
    max_files: int
    max_dirs: int
    max_dir_entries: int
    files_seen: int
    dirs_seen: int
    file_limited: bool
    dir_limited: bool
    entry_limited: bool
    read_failed: bool

    def budget(self) -> BoundedScanBudget:
        """One projection used identically for cold, warm and reopened indexes."""
        return BoundedScanBudget(**asdict(self))


@dataclass(frozen=True)
class FileIndexSnapshot:
    root: Path
    entries: tuple[FileIndexEntry, ...]
    directories: tuple[tuple[Path, int, int, int, str], ...]
    coverage: DiscoveryCoverage
    generation: str


def _payload_items(value: JsonValue) -> list[JsonValue]:
    if not isinstance(value, list):
        raise ValueError("snapshot entries must be an array")
    return value


def _coverage_from_payload(value: JsonValue) -> DiscoveryCoverage:
    raw = decode_json_object(value)
    coverage = DiscoveryCoverage(
        max_files=require_json_integer(raw["max_files"], "max_files"),
        max_dirs=require_json_integer(raw["max_dirs"], "max_dirs"),
        max_dir_entries=require_json_integer(raw["max_dir_entries"], "max_dir_entries"),
        files_seen=require_json_integer(raw["files_seen"], "files_seen"),
        dirs_seen=require_json_integer(raw["dirs_seen"], "dirs_seen"),
        file_limited=require_json_boolean(raw["file_limited"], "file_limited"),
        dir_limited=require_json_boolean(raw["dir_limited"], "dir_limited"),
        entry_limited=require_json_boolean(raw["entry_limited"], "entry_limited"),
        read_failed=require_json_boolean(raw["read_failed"], "read_failed"),
    )
    if raw.keys() != asdict(coverage).keys():
        raise ValueError("invalid coverage fields")
    return coverage


def _entry_from_payload(value: JsonValue) -> FileIndexEntry:
    raw = decode_json_object(value)
    return FileIndexEntry(
        Path(require_json_string(raw["path"], "path")),
        require_json_integer(raw["size"], "size"),
        require_json_integer(raw["mtime_ns"], "mtime_ns"),
        require_json_integer(raw["mode"], "mode"),
    )


def _directory_from_payload(value: JsonValue) -> tuple[Path, int, int, int, str]:
    path, modified, device, inode, signature = _payload_items(value)
    return (
        Path(require_json_string(path, "directory path")),
        require_json_integer(modified, "directory metadata mtime_ns"),
        require_json_integer(device, "directory metadata device"),
        require_json_integer(inode, "directory metadata inode"),
        require_json_string(signature, "directory membership signature"),
    )


class IncrementalFileIndex:
    """Owned by an execution context; persistent storage is explicitly supplied.

    Snapshots include discovery omissions, never content or execution results.
    Missing/stale snapshots rebuild; malformed/unreadable snapshots fail closed.
    """

    def __init__(self, state_home: Path | None = None) -> None:
        self._snapshots: dict[str, FileIndexSnapshot] = {}
        self._state_home = state_home.resolve() if state_home is not None else None
        self._lock = RLock()

    def discover(
        self,
        root: Path,
        start: Path,
        *,
        excluded_dirs: set[str],
        budget: BoundedScanBudget,
        skip_start_if_excluded: bool = True,
    ) -> FileIndexSnapshot:
        root, start = root.resolve(), start.resolve()
        start.relative_to(root)
        policy: list[object] = [
            str(root),
            str(start),
            sorted(name.lower() for name in excluded_dirs),
            skip_start_if_excluded,
            budget.max_files,
            budget.max_dirs,
            budget.max_dir_entries,
        ]
        key = hashlib.sha256(json.dumps([_SNAPSHOT_SCHEMA, policy], separators=(",", ":")).encode()).hexdigest()
        path = self._state_home / "search_index" / f"{key}.json" if self._state_home is not None else None
        with self._lock:
            cancellation.check()
            snapshot = self._snapshots.get(key)
            if snapshot is None and path is not None:
                snapshot = self._load(path, policy)
            stale = snapshot is None or self._changed(snapshot, excluded_dirs, budget)
            if stale:
                snapshot = self._refresh(start, excluded_dirs, budget, skip_start_if_excluded)
            else:
                assert snapshot is not None
                snapshot = FileIndexSnapshot(
                    snapshot.root,
                    tuple(self._entry(e.path) for e in snapshot.entries),
                    snapshot.directories,
                    snapshot.coverage,
                    snapshot.generation,
                )
            if path is not None:
                self._persist(path, policy, snapshot)
            self._snapshots[key] = snapshot
            return snapshot

    @staticmethod
    def _entry(path: Path) -> FileIndexEntry:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise OSError(f"indexed file changed type: {path}")
        return FileIndexEntry(path, info.st_size, info.st_mtime_ns, info.st_mode)

    @staticmethod
    def _directory(path: Path) -> tuple[Path, int, int, int]:
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise OSError(f"indexed directory changed type: {path}")
        return path, info.st_mtime_ns, info.st_dev, info.st_ino

    def _directory_record(self, path: Path, entries: DirectoryEntries) -> tuple[Path, int, int, int, str]:
        members = []
        for entry in entries.paths:
            cancellation.check()
            members.append((entry.name, stat.S_IFMT(entry.lstat().st_mode)))
        encoded = json.dumps([sorted(members), entries.entry_limited, entries.read_failed],
                             ensure_ascii=True, separators=(",", ":"))
        return (*self._directory(path), hashlib.sha256(encoded.encode()).hexdigest())

    def _current_directory(
        self, path: Path, excluded_dirs: set[str], limits: BoundedScanBudget,
    ) -> tuple[Path, int, int, int, str]:
        # Validate only previously visited directories, using the original
        # bounded collector. Timestamps alone cannot prove membership on NTFS.
        self._directory(path)  # Reject replaced directories/symlinks before enumeration.
        entries = collect_bounded_dir_entries(path, {name.lower() for name in excluded_dirs}, limits.max_dir_entries)
        if entries.read_failed:
            raise OSError(f"cannot validate indexed directory: {path}")
        return self._directory_record(path, entries)

    def _refresh(
        self, start: Path, excluded_dirs: set[str], limits: BoundedScanBudget, skip: bool
    ) -> FileIndexSnapshot:
        budget = BoundedScanBudget(
            max_files=limits.max_files, max_dirs=limits.max_dirs, max_dir_entries=limits.max_dir_entries
        )
        directories = []
        files = list(
            iter_bounded_files(
                start,
                excluded_dirs=excluded_dirs,
                budget=budget,
                skip_start_if_excluded=skip,
                on_directory=lambda path, entries: directories.append(self._directory_record(path, entries)),
            )
        )
        entries = tuple(sorted((self._entry(path) for path in files), key=lambda e: e.path.as_posix()))
        # Verify the actual bounded membership as well as identity and stamp.
        # A mutation during discovery must not certify an older candidate set.
        if any(self._current_directory(item[0], excluded_dirs, limits) != item for item in directories):
            raise OSError("workspace changed during indexed discovery; retry search")
        coverage = DiscoveryCoverage(
            max_files=budget.max_files,
            max_dirs=budget.max_dirs,
            max_dir_entries=budget.max_dir_entries,
            files_seen=budget.files_seen,
            dirs_seen=budget.dirs_seen,
            file_limited=budget.file_limited,
            dir_limited=budget.dir_limited,
            entry_limited=budget.entry_limited,
            read_failed=budget.read_failed,
        )
        return FileIndexSnapshot(start, entries, tuple(directories), coverage, uuid4().hex)

    def _changed(self, snapshot: FileIndexSnapshot, excluded_dirs: set[str], limits: BoundedScanBudget) -> bool:
        if snapshot.coverage.read_failed:
            return True
        try:
            if any(self._current_directory(item[0], excluded_dirs, limits) != item for item in snapshot.directories):
                return True
            # Single-file scopes have no directory walker. Also catch changes
            # of file kind even on filesystems with coarse directory timestamps.
            return any(not stat.S_ISREG(entry.path.lstat().st_mode) for entry in snapshot.entries)
        except FileNotFoundError:
            return True

    @staticmethod
    def _persist(path: Path, policy: list[object], snapshot: FileIndexSnapshot) -> None:
        payload = {
            "schema": _SNAPSHOT_SCHEMA,
            "policy": policy,
            "root": str(snapshot.root),
            "generation": snapshot.generation,
            "coverage": asdict(snapshot.coverage),
            "entries": [{**asdict(e), "path": str(e.path)} for e in snapshot.entries],
            "directories": [[str(p), m, d, i, signature] for p, m, d, i, signature in snapshot.directories],
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        write_json_atomic(path, {"snapshot": payload, "sha256": hashlib.sha256(encoded.encode()).hexdigest()})

    @staticmethod
    def _load(path: Path, policy: list[object]) -> FileIndexSnapshot | None:
        raw = read_json_strict(path)
        if raw is None:
            return None
        try:
            payload = decode_json_object(raw["snapshot"])
            encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            if raw["sha256"] != hashlib.sha256(encoded.encode()).hexdigest():
                raise ValueError("snapshot digest mismatch")
            if payload["schema"] != _SNAPSHOT_SCHEMA or payload["policy"] != policy or payload["root"] != policy[1]:
                raise ValueError("snapshot policy mismatch")
            coverage = _coverage_from_payload(payload["coverage"])
            for name, value in asdict(coverage).items():
                expected = bool if name.endswith(("limited", "failed")) else int
                # bool is not an integer budget.
                if type(value) is not expected or (expected is int and value < 0):
                    raise ValueError("invalid coverage")
            if [coverage.max_files, coverage.max_dirs, coverage.max_dir_entries] != policy[-3:]:
                raise ValueError("coverage limits mismatch")
            entries = tuple(
                _entry_from_payload(entry) for entry in _payload_items(payload["entries"])
            )
            directories = tuple(_directory_from_payload(row) for row in _payload_items(payload["directories"]))
            start = Path(str(policy[1]))
            for candidate in [e.path for e in entries] + [d[0] for d in directories]:
                if not candidate.is_absolute() or ".." in candidate.parts:
                    raise ValueError("invalid indexed path")
                candidate.relative_to(start)
            if any(type(v) is not int or v < 0 for e in entries for v in (e.size, e.mtime_ns, e.mode)):
                raise ValueError("invalid metadata")
            if any(type(v) is not int or v < 0 for item in directories for v in item[1:4]):
                raise ValueError("invalid directory metadata")
            if any(not isinstance(item[4], str) or len(item[4]) != 64
                   or any(char not in "0123456789abcdef" for char in item[4]) for item in directories):
                raise ValueError("invalid directory membership")
            if (len(entries) != coverage.files_seen or len(directories) != coverage.dirs_seen
                    or coverage.dirs_seen > coverage.max_dirs or any(not stat.S_ISREG(e.mode) for e in entries)):
                raise ValueError("inconsistent discovery coverage")
            generation = payload["generation"]
            if not isinstance(generation, str) or not generation:
                raise ValueError("missing generation")
            if len(entries) > coverage.files_seen or coverage.files_seen > coverage.max_files:
                raise ValueError("inconsistent file coverage")
            return FileIndexSnapshot(start, entries, directories, coverage, generation)
        except (KeyError, TypeError, ValueError) as exc:
            raise StoreCorruption(path, str(exc)) from exc
