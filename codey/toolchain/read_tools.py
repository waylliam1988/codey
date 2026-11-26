"""Bounded workspace reading, listing, search, and reference discovery."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

from codey.runtime.core import cancellation
from codey.toolchain.constants import SEARCH_EXCLUDED_DIRS
from codey.toolchain.edit_text import normalized_text
from codey.toolchain.outcome import ToolOutcome
from codey.toolchain.path_checks import (
    _checked_tool_path,
    _verify_dir_for_use,
    _verify_file_for_use,
    _verify_path_exists,
)
from codey.utils.references import find_reference_hints
from codey.utils.scan_report import render_scan_coverage
from codey.utils.text_budget import clip_middle
from codey.workspace.bounded_scan import (
    DEFAULT_MAX_DIR_ENTRIES,
    DEFAULT_MAX_SCAN_DIRS,
    DEFAULT_MAX_SCAN_FILES,
    BoundedScanBudget,
)
from codey.workspace.file_index import IncrementalFileIndex
from codey.workspace.paths import bounded_directory_entries as _bounded_directory_entries

SEARCH_MAX_RESULTS = 80
SEARCH_MAX_FILE_BYTES = 8 * 1024 * 1024
SEARCH_MAX_SCAN_BYTES = 16 * 1024 * 1024
SEARCH_MAX_SCAN_FILES = DEFAULT_MAX_SCAN_FILES
SEARCH_MAX_SCAN_DIRS = DEFAULT_MAX_SCAN_DIRS
SEARCH_MAX_DIR_ENTRIES = DEFAULT_MAX_DIR_ENTRIES
LIST_MAX_DIR_ENTRIES = DEFAULT_MAX_DIR_ENTRIES
LIST_MAX_SUBDIR_ENTRIES = 50
READ_DEFAULT_LINES = 300
READ_MAX_LINES = 600
READ_MAX_CHARS = 16_000
LONG_LINE_MARKER = "\n[... middle of overlong line omitted; not a complete old_string ...]\n"


def _dir_entry_kind(entry: Path) -> str:
    """Best-effort kind probe for directory listing display.

    Returns ``"dir"``, ``"file"``, or ``"unreadable"``. Never raises: an
    entry whose type cannot be verified renders as unreadable instead of
    failing the whole listing. Symlink semantics are unchanged (followed,
    as before); only verification failures are contained.
    """

    try:
        return "dir" if entry.is_dir() else "file"
    except (OSError, ValueError):
        return "unreadable"


def bounded_positive_int(
    value: object,
    name: str,
    maximum: int | None = None,
) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        parsed = value
    elif isinstance(value, str) and value.strip().isascii() and value.strip().isdigit():
        try:
            parsed = int(value.strip())
        except ValueError:
            raise ValueError(f"{name} must be a positive integer") from None
    else:
        raise ValueError(f"{name} must be a positive integer")
    if parsed < 1:
        raise ValueError(f"{name} must be a positive integer")
    if maximum is not None and parsed > maximum:
        raise ValueError(f"{name} must be at most {maximum}")
    return parsed


def _append_page_metadata(content: str, metadata: str) -> str:
    separator = "" if content.endswith("\n") else "\n"
    return f"{content}{separator}\n[{metadata}]"


def _read_file_next_call(rel: str, offset: int, limit: int) -> str:
    payload: dict[str, object] = {"tool": "read_file", "args": {"path": rel, "offset": offset, "limit": limit}}
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def read_file(
    root: Path,
    rel: str,
    *,
    offset: int = 1,
    limit: int = READ_DEFAULT_LINES,
) -> ToolOutcome:
    cancellation.check()
    path, error = _checked_tool_path(root, rel, tool="read_file")
    if error is not None or path is None:
        return error or ToolOutcome.error("path could not be resolved")
    # Final fail-closed check immediately before use (Windows has no
    # O_NOFOLLOW; any verification failure denies).
    verify_error = _verify_file_for_use(root, rel, path, tool="read_file")
    if verify_error is not None:
        return verify_error
    try:
        start_line = bounded_positive_int(offset, "offset")
        line_limit = bounded_positive_int(limit, "limit", READ_MAX_LINES)
    except ValueError as exc:
        return ToolOutcome.error(str(exc))
    # Streamed paging: keeps one page in memory. Lines are split on
    # LF/CRLF by the file iterator (universal newlines); other Unicode
    # line boundaries such as VT are not line breaks here by intent.
    # A single over-long line is still read fully before preview cut.
    lines: list[str] = []
    total = 0
    digest = hashlib.sha256()
    try:
        with path.open("r", encoding="utf-8", errors="strict", newline="") as handle:
            for raw in handle:
                digest.update(raw.encode("utf-8"))
                total += 1
                if total % 500 == 0:
                    cancellation.check()
                if start_line <= total < start_line + line_limit:
                    lines.append(normalized_text(raw))
        cancellation.check()
    except UnicodeDecodeError:
        return ToolOutcome.error(f"not utf-8 text: {rel}")
    except ValueError as exc:
        return ToolOutcome.error(str(exc))
    except OSError:
        return ToolOutcome.error(f"cannot read file: {rel}")
    result = _format_read_file_page(rel, start_line, line_limit, total, lines)
    if result.ok:
        return replace(result, canonical={**result.canonical, "file_version": "raw:sha256:" + digest.hexdigest()})
    return result


def _format_read_file_page(
    rel: str,
    start_line: int,
    line_limit: int,
    total: int,
    lines: list[str],
) -> ToolOutcome:
    if total == 0:
        if start_line != 1:
            return ToolOutcome.error(f"offset {start_line} exceeds {rel} total lines 0")
        return _read_page_outcome(rel, "", "", 1, 0, 0, text_complete=True)
    if start_line > total:
        return ToolOutcome.error(
            f"offset {start_line} exceeds {rel} total lines {total}"
        )

    available = lines
    first = available[0]
    if len(first) > READ_MAX_CHARS:
        preview, _ = clip_middle(first, READ_MAX_CHARS, LONG_LINE_MARKER)
        next_offset = start_line + 1
        next_text = ""
        if next_offset <= total:
            next_text = (
                f"; next offset={next_offset}; next call: "
                f"{_read_file_next_call(rel, next_offset, line_limit)}"
            )
        metadata = (
            f"read_file page: line {start_line} of {total} exceeds "
            f"{READ_MAX_CHARS} chars; preview only, not a complete old_string"
            f"{next_text}"
        )
        return _read_page_outcome(rel, preview, _append_page_metadata(preview, metadata),
                                  start_line, start_line, total, text_complete=False, truncated=True)

    selected: list[str] = []
    chars = 0
    for line in available:
        cancellation.check()
        if selected and chars + len(line) > READ_MAX_CHARS:
            break
        selected.append(line)
        chars += len(line)

    content = "".join(selected)
    end_line = start_line + len(selected) - 1
    partial = start_line > 1 or end_line < total
    if not partial:
        return _read_page_outcome(rel, content, content, start_line, end_line, total, text_complete=True)
    next_text = ""
    if end_line < total:
        next_offset = end_line + 1
        next_text = (
            f"; next offset={next_offset}; next call: "
            f"{_read_file_next_call(rel, next_offset, line_limit)}"
        )
    metadata = (
        f"read_file page: lines {start_line}-{end_line} of {total}{next_text}"
    )
    return _read_page_outcome(rel, content, _append_page_metadata(content, metadata),
                              start_line, end_line, total, text_complete=True, truncated=True)


def _read_page_outcome(rel: str, content: str, display: str, start: int, end: int, total: int,
    *, text_complete: bool, truncated: bool = False,
) -> ToolOutcome:
    value = {"path": rel, "text": content, "start_line": start, "end_line": end, "total_lines": total,
             "next_offset": end + 1 if end < total else 0, "text_complete": text_complete}
    return ToolOutcome(display, True, canonical={"value": value}, truncated=truncated,
                       presentation={"preview": {"line_start": start, "line_end": end}})


def list_directory(root: Path, rel: str) -> ToolOutcome:
    cancellation.check()
    path, error = _checked_tool_path(root, rel, tool="list_dir")
    if error is not None or path is None:
        return error or ToolOutcome.error("path could not be resolved")
    verify_error = _verify_dir_for_use(root, rel, path, tool="list_dir")
    if verify_error is not None:
        return verify_error
    lines: list[str] = []
    incomplete = False
    try:
        entries, entry_limited = _bounded_directory_entries(
            path,
            LIST_MAX_DIR_ENTRIES,
            include_hidden=False,
            check_cancel=cancellation.check,
        )
    except (OSError, ValueError):
        return ToolOutcome.error(f"cannot list directory: {rel}")
    limited = entry_limited
    for entry in entries:
        cancellation.check()
        kind = _dir_entry_kind(entry)
        if kind == "unreadable":
            lines.append(f"{entry.name} (unreadable)")
            incomplete = True
            continue
        if kind == "dir":
            lines.append(f"{entry.name}/")
            try:
                sub_entries, sub_limited = _bounded_directory_entries(
                    entry,
                    LIST_MAX_SUBDIR_ENTRIES,
                    include_hidden=False,
                    check_cancel=cancellation.check,
                )
            except (OSError, ValueError):
                lines.append("  (unreadable)")
                incomplete = True
                continue
            for sub in sub_entries:
                cancellation.check()
                sub_kind = _dir_entry_kind(sub)
                if sub_kind == "unreadable":
                    lines.append(f"  {sub.name} (unreadable)")
                    incomplete = True
                    continue
                tag = "/" if sub_kind == "dir" else ""
                lines.append(f"  {sub.name}{tag}")
            if sub_limited:
                limited = True
                lines.append(
                    f"  ... list_dir stopped after {LIST_MAX_SUBDIR_ENTRIES} directory entries"
                )
        else:
            lines.append(entry.name)
    if entry_limited:
        lines.append(f"... list_dir stopped after {LIST_MAX_DIR_ENTRIES} directory entries")
    return ToolOutcome("\n".join(lines) if lines else "(empty)", True, truncated=incomplete or limited)


def search_files(
    root: Path, rel: str, query: str, *, offset: int = 1, limit: int = SEARCH_MAX_RESULTS,
    max_results: int | None = None, literal: bool = True, case_sensitive: bool = False, include: str = "",
    exclude: tuple[str, ...] | list[str] = (), before_context: int = 0, after_context: int = 0,
    file_index: IncrementalFileIndex | None = None,
) -> ToolOutcome:
    from codey.toolchain.rg_search import search
    from codey.toolchain.search_page import normalize_page_args

    cancellation.check()
    if not query.strip():
        return ToolOutcome.error("search query required")
    page_offset, page_limit = normalize_page_args(offset, limit, max_results, SEARCH_MAX_RESULTS)
    start, error = _checked_tool_path(root, rel or ".", tool="grep")
    if error is not None or start is None:
        return error or ToolOutcome.error("path could not be resolved")
    verify_error = _verify_path_exists(root, rel or ".", start, tool="grep")
    if verify_error is not None:
        return verify_error
    root = root.resolve()
    budget = BoundedScanBudget(max_files=SEARCH_MAX_SCAN_FILES, max_dirs=SEARCH_MAX_SCAN_DIRS,
                               max_dir_entries=SEARCH_MAX_DIR_ENTRIES)
    index = file_index if file_index is not None else IncrementalFileIndex()
    try:
        snapshot = index.discover(root, start, excluded_dirs=SEARCH_EXCLUDED_DIRS, budget=budget,
                                  skip_start_if_excluded=start != root)
    except (OSError, ValueError) as exc:
        return ToolOutcome.error(f"search index failed: {exc}", error_code="search_index_failed")
    files = [entry.path for entry in snapshot.entries]
    budget = snapshot.coverage.budget()
    return search(root, files, budget, query=query, path=rel or ".", offset=page_offset, limit=page_limit,
                  literal=literal, case_sensitive=case_sensitive, include=include, exclude=exclude,
                  before_context=before_context, after_context=after_context,
                  max_file_bytes=SEARCH_MAX_FILE_BYTES, max_scan_bytes=SEARCH_MAX_SCAN_BYTES)


def find_references(root: Path, rel: str, symbol: str) -> ToolOutcome:
    symbol = (symbol or "").strip()
    if not symbol:
        return ToolOutcome.error("symbol required")
    start, error = _checked_tool_path(root, rel or ".", tool="find_references")
    if error is not None or start is None:
        return error or ToolOutcome.error("path could not be resolved")
    verify_error = _verify_path_exists(root, rel or ".", start, tool="find_references")
    if verify_error is not None:
        return verify_error
    try:
        scan = find_reference_hints(root, start, symbol)
    except ValueError as exc:
        return ToolOutcome.error(str(exc))
    coverage = render_scan_coverage(scan.report) if scan.report is not None else ""
    output = f"{scan.output}\n{coverage}" if coverage else scan.output
    incomplete = bool(scan.report and scan.report.incomplete)
    return ToolOutcome(output, True, truncated=scan.truncated or incomplete)
