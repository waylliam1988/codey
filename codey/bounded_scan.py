"""Bounded local file traversal helpers.

This module only walks file paths. It does not read file contents, build an
index, cache results, or infer semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator


DEFAULT_MAX_SCAN_FILES = 1_000
DEFAULT_MAX_SCAN_DIRS = 250
DEFAULT_MAX_DIR_ENTRIES = 1_000


@dataclass
class BoundedScanBudget:
    max_files: int = DEFAULT_MAX_SCAN_FILES
    max_dirs: int = DEFAULT_MAX_SCAN_DIRS
    max_dir_entries: int = DEFAULT_MAX_DIR_ENTRIES
    files_seen: int = 0
    dirs_seen: int = 0
    file_limited: bool = False
    dir_limited: bool = False
    entry_limited: bool = False

    @property
    def limited(self) -> bool:
        return self.file_limited or self.dir_limited or self.entry_limited

    def stop_message(self, label: str) -> str:
        reasons: list[str] = []
        if self.file_limited:
            reasons.append(f"file budget {self.max_files}")
        if self.dir_limited:
            reasons.append(f"directory budget {self.max_dirs}")
        if self.entry_limited:
            reasons.append(f"per-directory entry budget {self.max_dir_entries}")
        reason = ", ".join(reasons) if reasons else "scan budget"
        return (
            f"- {label} stopped after {self.files_seen} files and "
            f"{self.dirs_seen} directories ({reason}); omitted files may "
            "contain more matches"
        )


def iter_provided_files(
    files: Iterable[Path],
    budget: BoundedScanBudget,
) -> Iterator[Path]:
    for path in files:
        if budget.files_seen >= budget.max_files:
            budget.file_limited = True
            return
        budget.files_seen += 1
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
    excluded_lower = {name.lower() for name in excluded_dirs}
    if start.is_symlink():
        return
    if start.is_file():
        if allow_file is not None and not allow_file(start):
            return
        if budget.files_seen >= budget.max_files:
            budget.file_limited = True
            return
        budget.files_seen += 1
        yield start
        return
    if skip_start_if_excluded and start.name.lower() in excluded_lower:
        return

    stack = [start]
    while stack:
        current = stack.pop()
        if budget.dirs_seen >= budget.max_dirs:
            budget.dir_limited = True
            return
        budget.dirs_seen += 1
        entries: list[Path] = []
        try:
            for index, entry in enumerate(current.iterdir()):
                if index >= budget.max_dir_entries:
                    budget.entry_limited = True
                    break
                entries.append(entry)
        except OSError:
            continue

        dirs: list[Path] = []
        for entry in sorted(entries, key=lambda path: path.name.lower()):
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
                    if budget.files_seen >= budget.max_files:
                        budget.file_limited = True
                        return
                    budget.files_seen += 1
                    yield entry
            except OSError:
                continue
        stack.extend(reversed(dirs))
