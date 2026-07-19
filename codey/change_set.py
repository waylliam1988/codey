"""Structured view of a collected project changes dict."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Mapping, Sequence


MAX_SUMMARY_FILES = 20
MAX_SUMMARY_HUNKS = 60
HUNK_RE = re.compile(
    r"^@@\s+-(?P<old_start>\d+)(?:,(?P<old_lines>\d+))?\s+"
    r"\+(?P<new_start>\d+)(?:,(?P<new_lines>\d+))?\s+@@(?P<header>.*)$"
)


@dataclass(frozen=True)
class ChangeHunk:
    index: int
    old_start: int
    old_lines: int
    new_start: int
    new_lines: int
    header: str

    def contains_old_line(self, line: int) -> bool:
        return _contains_line(line, self.old_start, self.old_lines)

    def contains_new_line(self, line: int) -> bool:
        return _contains_line(line, self.new_start, self.new_lines)


@dataclass(frozen=True)
class ChangeFile:
    path: str
    status: str = "M"
    additions: int = 0
    deletions: int = 0
    hunks: tuple[ChangeHunk, ...] = ()
    previous_path: str = ""


@dataclass(frozen=True)
class ChangeAnchor:
    hunk_index: int | None = None
    new_line: int | None = None
    old_line: int | None = None


@dataclass(frozen=True)
class ChangeSet:
    ok: bool
    mode: str = ""
    root: str = ""
    files: tuple[ChangeFile, ...] = ()
    changed_count: int = 0
    raw_diff: str = ""
    raw_diff_truncated: bool = False
    error: str = ""

    @classmethod
    def from_changes(cls, changes: Mapping[str, object] | object) -> "ChangeSet":
        if not isinstance(changes, Mapping):
            return cls(ok=False, error="invalid changes")
        raw_diff = _text(changes.get("diff"))
        files = _files_from_changes(changes)
        hunks_by_path = _parse_hunks(raw_diff)
        files = tuple(
            ChangeFile(
                path=file.path,
                status=file.status,
                additions=file.additions,
                deletions=file.deletions,
                hunks=hunks_by_path.get(file.path, ()),
                previous_path=file.previous_path,
            )
            for file in files
        )
        try:
            changed_count = int(changes.get("changed_count") or len(files))
        except (TypeError, ValueError):
            changed_count = len(files)
        return cls(
            ok=bool(changes.get("ok")),
            mode=_text(changes.get("mode")),
            root=_text(changes.get("root")),
            files=files,
            changed_count=max(0, changed_count),
            raw_diff=raw_diff,
            raw_diff_truncated=bool(changes.get("truncated")),
            error=_text(changes.get("error")),
        )

    def changed_paths(self) -> tuple[str, ...]:
        return tuple(file.path for file in self.files)

    def has_reviewable_diff(self) -> bool:
        return bool(self.ok and self.changed_count > 0 and self.raw_diff.strip())

    def render_summary(
        self,
        *,
        max_files: int = MAX_SUMMARY_FILES,
        max_hunks: int = MAX_SUMMARY_HUNKS,
    ) -> str:
        lines = ["ChangeSet Summary (structure before raw diff):"]
        if not self.ok:
            lines.append(f"- unavailable: {self.error or 'change collection failed'}")
            return "\n".join(lines)
        if not self.files:
            lines.append("- no changed files")
            return "\n".join(lines)
        hunk_count = 0
        for file in self.files[:max_files]:
            lines.append(
                f"- {file.status or 'M'} {file.path} +{file.additions} -{file.deletions}"
            )
            for hunk in file.hunks:
                if hunk_count >= max_hunks:
                    lines.append("  - hunk list truncated")
                    return "\n".join(lines)
                lines.append(f"  - hunk {hunk.index}: {hunk.header}")
                hunk_count += 1
        if len(self.files) > max_files:
            lines.append("- file list truncated")
        if self.raw_diff_truncated:
            lines.append("- raw diff was truncated before review")
        return "\n".join(lines)

    def normalize_anchor(
        self,
        path: object,
        hunk_index: object = None,
        new_line: object = None,
        old_line: object = None,
    ) -> ChangeAnchor:
        file = self.file_for_path(path)
        if file is None or not file.hunks:
            return ChangeAnchor()
        hunk_number = _positive_int(hunk_index)
        new_number = _positive_int(new_line)
        old_number = _positive_int(old_line)
        hunk = _hunk_by_index(file.hunks, hunk_number) if hunk_number else None
        if hunk_number and hunk is None:
            hunk_number = None
        if hunk is not None:
            if new_number is not None and not hunk.contains_new_line(new_number):
                new_number = None
            if old_number is not None and not hunk.contains_old_line(old_number):
                old_number = None
            return ChangeAnchor(hunk_number, new_number, old_number)
        if new_number is not None:
            matched = _first_hunk_containing_new_line(file.hunks, new_number)
            if matched is not None:
                hunk = matched
            else:
                new_number = None
        if old_number is not None:
            matched = _first_hunk_containing_old_line(file.hunks, old_number)
            if matched is not None and (hunk is None or matched.index == hunk.index):
                hunk = matched
            else:
                old_number = None
        return ChangeAnchor(
            hunk.index if hunk is not None else None,
            new_number,
            old_number,
        )

    def file_for_path(self, path: object) -> ChangeFile | None:
        target, _previous = _change_file_paths(_text(path), "")
        if not target:
            return None
        for file in self.files:
            if file.path == target:
                return file
        return None


def _files_from_changes(changes: Mapping[str, object]) -> tuple[ChangeFile, ...]:
    files: list[ChangeFile] = []
    seen: set[str] = set()
    raw_files = changes.get("files")
    if not isinstance(raw_files, Sequence) or isinstance(raw_files, (str, bytes)):
        return ()
    for item in raw_files:
        if not isinstance(item, Mapping):
            continue
        path, previous_path = _change_file_paths(
            _text(item.get("path")),
            _text(item.get("previous_path")),
        )
        if not path or path in seen:
            continue
        seen.add(path)
        files.append(
            ChangeFile(
                path=path,
                status=_text(item.get("status") or "M")[:20],
                additions=_nonnegative_int(item.get("additions")),
                deletions=_nonnegative_int(item.get("deletions")),
                previous_path=previous_path,
            )
        )
    return tuple(files)


def _parse_hunks(diff: str) -> dict[str, tuple[ChangeHunk, ...]]:
    result: dict[str, list[ChangeHunk]] = {}
    current_path = ""
    current_hunks: list[ChangeHunk] | None = None
    pending_old = ""
    pending_new = ""
    for line in diff.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.startswith("diff --git "):
            current_path = _path_from_diff_git(line)
            current_hunks = result.setdefault(current_path, []) if current_path else None
            pending_old = ""
            pending_new = ""
            continue
        if line.startswith("--- "):
            pending_old = _path_from_file_header(line[4:])
            continue
        if line.startswith("+++ "):
            pending_new = _path_from_file_header(line[4:])
            selected = pending_new or pending_old or current_path
            if selected and selected != current_path:
                current_path = selected
                current_hunks = result.setdefault(current_path, [])
            continue
        if current_hunks is None or not line.startswith("@@"):
            continue
        hunk = _parse_hunk_line(line, len(current_hunks) + 1)
        if hunk is not None:
            current_hunks.append(hunk)
    return {path: tuple(hunks) for path, hunks in result.items() if path and hunks}


def _parse_hunk_line(line: str, index: int) -> ChangeHunk | None:
    match = HUNK_RE.match(line)
    if match is None:
        return None
    old_start = int(match.group("old_start"))
    old_lines = int(match.group("old_lines") or "1")
    new_start = int(match.group("new_start"))
    new_lines = int(match.group("new_lines") or "1")
    return ChangeHunk(
        index=index,
        old_start=old_start,
        old_lines=old_lines,
        new_start=new_start,
        new_lines=new_lines,
        header=line,
    )


def _path_from_diff_git(line: str) -> str:
    parts = line.split()
    if len(parts) >= 4:
        return _strip_diff_prefix(parts[3])
    return ""


def _path_from_file_header(value: str) -> str:
    token = value.split("\t", 1)[0].strip()
    if token == "/dev/null":
        return ""
    return _strip_diff_prefix(token)


def _strip_diff_prefix(path: str) -> str:
    value = path.strip().strip('"')
    if value.startswith("a/") or value.startswith("b/"):
        value = value[2:]
    return _safe_relpath(value)


def _change_file_paths(raw_path: str, raw_previous_path: str) -> tuple[str, str]:
    previous_path = _safe_relpath(raw_previous_path)
    if " -> " not in raw_path:
        return _safe_relpath(raw_path), previous_path
    before, after = raw_path.split(" -> ", 1)
    path = _safe_relpath(after)
    if not path:
        return _safe_relpath(raw_path), previous_path
    return path, previous_path or _safe_relpath(before)


def _safe_relpath(value: str) -> str:
    normalized = value.replace("\\", "/").strip().strip("/")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts:
        return ""
    return path.as_posix()


def _text(value: object) -> str:
    return "" if value is None else str(value)


def _nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _contains_line(line: int, start: int, count: int) -> bool:
    if count <= 0:
        return False
    return start <= line < start + count


def _hunk_by_index(hunks: tuple[ChangeHunk, ...], index: int | None) -> ChangeHunk | None:
    if index is None:
        return None
    for hunk in hunks:
        if hunk.index == index:
            return hunk
    return None


def _first_hunk_containing_new_line(
    hunks: tuple[ChangeHunk, ...],
    line: int,
) -> ChangeHunk | None:
    for hunk in hunks:
        if hunk.contains_new_line(line):
            return hunk
    return None


def _first_hunk_containing_old_line(
    hunks: tuple[ChangeHunk, ...],
    line: int,
) -> ChangeHunk | None:
    for hunk in hunks:
        if hunk.contains_old_line(line):
            return hunk
    return None
