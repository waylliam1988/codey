"""Bounded lexical reference hints.

This is not an index, LSP, call graph, rename engine, or semantic resolver. It
only returns stable text matches that help the agent decide which files to read.
"""

from __future__ import annotations

import re
import stat
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from codey.utils.scan_report import ScanReport
from codey.workspace.bounded_scan import (
    DEFAULT_MAX_DIR_ENTRIES,
    DEFAULT_MAX_SCAN_DIRS,
    DEFAULT_MAX_SCAN_FILES,
    BoundedScanBudget,
    iter_bounded_files,
    iter_provided_files,
)

REFERENCE_MAX_RESULTS = 80
REFERENCE_MAX_FILE_BYTES = 512 * 1024
REFERENCE_MAX_LINE_CHARS = 240
REFERENCE_MAX_SCAN_FILES = DEFAULT_MAX_SCAN_FILES
REFERENCE_MAX_SCAN_DIRS = DEFAULT_MAX_SCAN_DIRS
REFERENCE_MAX_DIR_ENTRIES = DEFAULT_MAX_DIR_ENTRIES
REFERENCE_EXCLUDED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "dist",
    "build",
    ".next",
    ".turbo",
    "target",
}
SIMPLE_SYMBOL_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
IDENTIFIER_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_$")


@dataclass(frozen=True)
class ReferenceScan:
    output: str
    truncated: bool = False
    report: ScanReport | None = None


def validate_symbol(symbol: object) -> str:
    clean = str(symbol or "").strip()
    if not SIMPLE_SYMBOL_RE.fullmatch(clean):
        raise ValueError(
            "find_references requires a simple symbol like createRouter, "
            "SessionStore, login_user, or $state; use grep for complex expressions"
        )
    return clean


def _iter_budgeted_files(files: Iterable[Path]) -> Iterator[Path]:
    """Yield caller-budgeted files without following symlinks.

    Mirrors the symlink rule of ``BoundedScanBudget.consume_file`` (lstat
    based, never follows links) but consumes no budget: the caller already
    accounted for these files. Entries without ``lstat`` (test doubles) and
    entries whose ``lstat`` fails are handled downstream, exactly like the
    non-budgeted path silently skips what ``consume_file`` rejects.
    """
    for path in files:
        lstat = getattr(path, "lstat", None)
        if not callable(lstat):
            yield path
            continue
        try:
            is_link = stat.S_ISLNK(lstat().st_mode)
        except OSError:
            continue
        if is_link:
            continue
        yield path


def _resolve_reference_candidate(
    path: Path,
    root: Path,
    report: ScanReport,
) -> tuple[Path | None, str | None]:
    resolve = getattr(path, "resolve", None)
    if callable(resolve):
        try:
            path = resolve()
        except OSError:
            rel = _relative(root, path)
            if rel:
                report.add_unreadable(rel)
            return None, None
    rel = _relative(root, path)
    if not rel:
        return None, None
    try:
        is_file = path.is_file()
    except OSError:
        report.add_unreadable(rel)
        return None, None
    if not is_file:
        return None, None
    try:
        size = path.stat().st_size
    except OSError:
        report.add_unreadable(rel)
        return None, None
    if size > REFERENCE_MAX_FILE_BYTES:
        report.add_oversized(rel)
        return None, None
    return path, rel


def _read_reference_text(path: Path, rel: str, report: ScanReport) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        report.add_decode_failed(rel)
        return None
    except OSError:
        report.add_unreadable(rel)
        return None


def _scan_reference_lines(
    text: str,
    *,
    rel: str,
    clean_symbol: str,
    pattern: re.Pattern[str],
    rows: list[str],
    max_results: int,
) -> bool:
    for line_no, line in enumerate(text.splitlines(), start=1):
        if not _has_bounded_symbol(pattern, line):
            continue
        if len(rows) >= max_results:
            return True
        clean_line = _clean_line(line)
        kind = _classify_reference(clean_line, clean_symbol)
        rows.append(f"- {kind} {rel}:{line_no}: {clean_line}")
    return False


def _render_reference_output(
    *,
    clean_symbol: str,
    start_label: str,
    rows: list[str],
    max_results: int,
    budget: BoundedScanBudget,
    truncated: bool,
) -> str:
    output = [
        f"References for {clean_symbol} under {start_label}:",
        "- reference hints only; lexical scan, not semantic resolution",
        "- use read_file before editing",
    ]
    output.extend(rows if rows else ["- no lexical matches found"])
    if truncated:
        if len(rows) >= max_results:
            output.append(f"- references truncated after {max_results} matches")
        if budget.limited:
            output.append(budget.stop_message("reference scan"))
    return "\n".join(output)


def find_reference_hints(
    root: Path,
    start: Path,
    symbol: object,
    *,
    files: Iterable[Path] | None = None,
    max_results: int = REFERENCE_MAX_RESULTS,
    max_scan_files: int | None = None,
    max_scan_dirs: int | None = None,
    max_dir_entries: int | None = None,
    scan_budget: BoundedScanBudget | None = None,
    files_budgeted: bool = False,
) -> ReferenceScan:
    clean_symbol = validate_symbol(symbol)
    root = root.resolve()
    start = start.resolve()
    budget = scan_budget or BoundedScanBudget(
        max_files=max_scan_files or REFERENCE_MAX_SCAN_FILES,
        max_dirs=max_scan_dirs or REFERENCE_MAX_SCAN_DIRS,
        max_dir_entries=max_dir_entries or REFERENCE_MAX_DIR_ENTRIES,
    )
    if files is not None:
        candidates = _iter_budgeted_files(files) if files_budgeted else iter_provided_files(files, budget)
    else:
        candidates = iter_bounded_files(
            start,
            excluded_dirs=REFERENCE_EXCLUDED_DIRS,
            budget=budget,
            skip_start_if_excluded=start != root,
        )
    pattern = _symbol_pattern(clean_symbol)
    rows: list[str] = []
    truncated = False
    report = ScanReport("reference scan", size_limit_bytes=REFERENCE_MAX_FILE_BYTES)

    for path in candidates:
        scan_path, rel = _resolve_reference_candidate(path, root, report)
        if scan_path is None or rel is None:
            continue
        text = _read_reference_text(scan_path, rel, report)
        if text is None:
            continue
        if _scan_reference_lines(
            text,
            rel=rel,
            clean_symbol=clean_symbol,
            pattern=pattern,
            rows=rows,
            max_results=max_results,
        ):
            truncated = True
            break
        if truncated:
            break
    if budget.limited:
        truncated = True

    start_label = _relative(root, start) or "."
    output = _render_reference_output(
        clean_symbol=clean_symbol,
        start_label=start_label,
        rows=rows,
        max_results=max_results,
        budget=budget,
        truncated=truncated,
    )
    return ReferenceScan(output, truncated, report)


def _symbol_pattern(symbol: str) -> re.Pattern[str]:
    return re.compile(re.escape(symbol))


def _classify_reference(line: str, symbol: str) -> str:
    escaped = re.escape(symbol)

    if re.search(r"^\s*(?:from\s+\S+\s+import\b.*|import\b.*)", line) and _has_bounded_symbol(
        _symbol_pattern(symbol),
        line,
    ):
        return "import"
    if re.search(r"^\s*export\b", line) and _has_bounded_symbol(_symbol_pattern(symbol), line):
        return "export"
    if re.search(rf"^\s*(?:async\s+)?def\s+{escaped}\s*\(", line):
        return "definition"
    if re.search(rf"^\s*class\s+{escaped}\b", line):
        return "definition"
    if re.search(
        rf"^\s*(?:async\s+)?function\s+{escaped}\s*\(",
        line,
    ):
        return "definition"
    if re.search(
        rf"^\s*(?:const|let|var)\s+{escaped}\s*=",
        line,
    ):
        return "definition"
    if _has_bounded_symbol_call(_symbol_pattern(symbol), line):
        return "call"
    return "reference"


def _has_bounded_symbol(pattern: re.Pattern[str], line: str) -> bool:
    return any(_match_has_identifier_boundaries(line, match.start(), match.end()) for match in pattern.finditer(line))


def _has_bounded_symbol_call(pattern: re.Pattern[str], line: str) -> bool:
    for match in pattern.finditer(line):
        if not _match_has_identifier_boundaries(line, match.start(), match.end()):
            continue
        rest = line[match.end() :].lstrip()
        if rest.startswith("("):
            return True
    return False


def _match_has_identifier_boundaries(line: str, start: int, end: int) -> bool:
    before_ok = start <= 0 or line[start - 1] not in IDENTIFIER_CHARS
    after_ok = end >= len(line) or line[end] not in IDENTIFIER_CHARS
    return before_ok and after_ok


def _clean_line(line: str) -> str:
    clean = line.strip()
    if len(clean) > REFERENCE_MAX_LINE_CHARS:
        return clean[: REFERENCE_MAX_LINE_CHARS - 3].rstrip() + "..."
    return clean


def _relative(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return ""
