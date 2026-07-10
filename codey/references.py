"""Bounded lexical reference hints.

This is not an index, LSP, call graph, rename engine, or semantic resolver. It
only returns stable text matches that help the agent decide which files to read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from codey.bounded_scan import (
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
IDENTIFIER_CHARS = r"A-Za-z0-9_$"


@dataclass(frozen=True)
class ReferenceScan:
    output: str
    truncated: bool = False


def validate_symbol(symbol: object) -> str:
    clean = str(symbol or "").strip()
    if not SIMPLE_SYMBOL_RE.fullmatch(clean):
        raise ValueError(
            "find_references requires a simple symbol like createRouter, "
            "SessionStore, login_user, or $state; use grep for complex expressions"
        )
    return clean


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
        candidates = files if scan_budget is not None else iter_provided_files(files, budget)
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

    for path in candidates:
        try:
            if not path.is_file() or path.stat().st_size > REFERENCE_MAX_FILE_BYTES:
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        rel = _relative(root, path)
        if not rel:
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            if not pattern.search(line):
                continue
            if len(rows) >= max_results:
                truncated = True
                break
            clean_line = _clean_line(line)
            kind = _classify_reference(clean_line, clean_symbol)
            rows.append(f"- {kind} {rel}:{line_no}: {clean_line}")
        if truncated:
            break
    if budget.limited:
        truncated = True

    start_label = _relative(root, start) or "."
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
    return ReferenceScan("\n".join(output), truncated)


def _symbol_pattern(symbol: str) -> re.Pattern[str]:
    return re.compile(
        rf"(?<![{IDENTIFIER_CHARS}]){re.escape(symbol)}(?![{IDENTIFIER_CHARS}])"
    )


def _classify_reference(line: str, symbol: str) -> str:
    escaped = re.escape(symbol)
    boundary_before = rf"(?<![{IDENTIFIER_CHARS}])"
    boundary_after = rf"(?![{IDENTIFIER_CHARS}])"
    exact = rf"{boundary_before}{escaped}{boundary_after}"

    if re.search(rf"^\s*(?:from\s+\S+\s+import\b.*|import\b.*){exact}", line):
        return "import"
    if re.search(rf"^\s*import\b.*{exact}", line):
        return "import"
    if re.search(rf"^\s*export\b.*{exact}", line):
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
    if re.search(rf"{exact}\s*\(", line):
        return "call"
    return "reference"


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
