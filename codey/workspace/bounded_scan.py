"""Bounded local file traversal helpers.

This module only walks file paths. It does not read file contents, build an
index, cache results, or infer semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator

from codey.runtime import cancellation


DEFAULT_MAX_SCAN_FILES = 1_000
DEFAULT_MAX_SCAN_DIRS = 250
DEFAULT_MAX_DIR_ENTRIES = 1_000


@dataclass
class BoundedScanBudget:
    max_files: int = DEFAULT_MAX_SCAN_FILES
    max_dirs: int = DEFAULT_MAX_SCAN_DIRS
    max_dir_entries: int = DEFAULT_MAX_DIR_ENTRIES
    max_bytes: int | None = None
    files_seen: int = 0
    dirs_seen: int = 0
    bytes_seen: int = 0
    file_limited: bool = False
    dir_limited: bool = False
    entry_limited: bool = False
    byte_limited: bool = False

    @property
    def limited(self) -> bool:
        return (
            self.file_limited
            or self.dir_limited
            or self.entry_limited
            or self.byte_limited
        )

    def stop_message(self, label: str) -> str:
        reasons: list[str] = []
        if self.file_limited:
            reasons.append(f"file budget {self.max_files}")
        if self.dir_limited:
            reasons.append(f"directory budget {self.max_dirs}")
        if self.entry_limited:
            reasons.append(f"per-directory entry budget {self.max_dir_entries}")
        if self.byte_limited and self.max_bytes is not None:
            reasons.append(f"byte budget {self.max_bytes}")
        reason = ", ".join(reasons) if reasons else "scan budget"
        scanned = f"{self.files_seen} files and {self.dirs_seen} directories"
        if self.max_bytes is not None:
            scanned += f", {self.bytes_seen} bytes"
        return (
            f"- {label} stopped after {scanned} ({reason}); omitted files may "
            "contain more matches"
        )

    def consume_file(self, path: Path) -> bool:
        if self.files_seen >= self.max_files:
            self.file_limited = True
            return False
        size = 0
        if self.max_bytes is not None:
            try:
                size = path.stat().st_size
            except OSError:
                return False
            if self.bytes_seen + size > self.max_bytes:
                self.byte_limited = True
                return False
        self.files_seen += 1
        self.bytes_seen += size
        return True


def iter_provided_files(
    files: Iterable[Path],
    budget: BoundedScanBudget,
) -> Iterator[Path]:
    for path in files:
        cancellation.check()
        if not budget.consume_file(path):
            if budget.limited:
                return
            continue
        yield path


def iter_bounded_files(
    start: Path,
    *,
    excluded_dirs: set[str],
    budget: BoundedScanBudget,
    allow_dir: Callable[[Path], bool] | None = None,
    allow_file: Callable[[Path], bool] | None = None,
    skip_start_if_excluded: bool = True,
) -> Iterator[Path]:
    cancellation.check()
    excluded_lower = {name.lower() for name in excluded_dirs}
    if start.is_symlink():
        return
    if start.is_file():
        if allow_file is not None and not allow_file(start):
            return
        if not budget.consume_file(start):
            return
        yield start
        return
    if skip_start_if_excluded and start.name.lower() in excluded_lower:
        return

    stack = [start]
    while stack:
        cancellation.check()
        current = stack.pop()
        if budget.dirs_seen >= budget.max_dirs:
            budget.dir_limited = True
            return
        budget.dirs_seen += 1
        entries: list[Path] = []
        try:
            for index, entry in enumerate(current.iterdir()):
                cancellation.check()
                if index >= budget.max_dir_entries:
                    budget.entry_limited = True
                    break
                entries.append(entry)
        except OSError:
            continue

        dirs: list[Path] = []
        for entry in sorted(entries, key=lambda path: path.name.lower()):
            cancellation.check()
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    if entry.name.lower() in excluded_lower:
                        continue
                    if allow_dir is not None and not allow_dir(entry):
                        continue
                    dirs.append(entry)
                elif entry.is_file():
                    if allow_file is not None and not allow_file(entry):
                        continue
                    if not budget.consume_file(entry):
                        if budget.limited:
                            return
                        continue
                    yield entry
            except OSError:
                continue
        stack.extend(reversed(dirs))