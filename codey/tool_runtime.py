"""Local project tools with structured outcomes."""

from __future__ import annotations

import ast
import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from codey import cancellation
from codey.bounded_scan import (
    DEFAULT_MAX_DIR_ENTRIES,
    DEFAULT_MAX_SCAN_DIRS,
    DEFAULT_MAX_SCAN_FILES,
    BoundedScanBudget,
    iter_bounded_files,
)
from codey.references import find_reference_hints
from codey.text_budget import clip_middle, prune_dependency_stack_frames


SEARCH_EXCLUDED_DIRS = {
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
SEARCH_MAX_RESULTS = 80
SEARCH_MAX_FILE_BYTES = 8 * 1024 * 1024
SEARCH_MAX_SCAN_BYTES = 16 * 1024 * 1024
SEARCH_MAX_SCAN_FILES = DEFAULT_MAX_SCAN_FILES
SEARCH_MAX_SCAN_DIRS = DEFAULT_MAX_SCAN_DIRS
SEARCH_MAX_DIR_ENTRIES = DEFAULT_MAX_DIR_ENTRIES
WRITE_MAX_FILE_BYTES = 512 * 1024
READ_DEFAULT_LINES = 300
READ_MAX_LINES = 600
READ_MAX_CHARS = 16_000
MAX_REPLACEMENTS = 8
EDIT_FAILURE_MAX_CHARS = 1_600
EDIT_FAILURE_MAX_LINES = 7
EDIT_FAILURE_MAX_MATCHES = 3
EDIT_FAILURE_MAX_LINE_CHARS = 400
EDIT_FAILURE_MAX_ANCHOR_CANDIDATES = 32
PYTHON_SYNTAX_HINT_MAX_CHARS = 128 * 1024
PYTHON_SYNTAX_HINT_MAX_MESSAGE_CHARS = 160
RUN_TIMEOUT_SECONDS = 90
RUN_OUTPUT_LIMIT = 24_000
RUN_FORBIDDEN_TOKENS = {"&&", "||", ";", "|", ">", ">>", "<", "$(", "`"}
RUN_ALLOWED_PYTHON_FLAGS = {"-B"}
RUN_ALLOWED_NPM_SCRIPTS = {"test", "build", "lint", "check", "typecheck"}
LONG_LINE_MARKER = "\n[... middle of overlong line omitted; not a complete old_string ...]\n"
EDIT_ANCHOR_RE = re.compile(
    r"(?P<quote>['\"])(?P<literal>[^'\"\r\n]{4,})(?P=quote)"
    r"|(?P<identifier>[A-Za-z_][A-Za-z0-9_]{3,})"
)
EDIT_ANCHOR_STOPWORDS = {
    "class",
    "const",
    "else",
    "false",
    "function",
    "import",
    "return",
    "true",
}


@dataclass(frozen=True)
class ToolOutcome:
    output: str
    ok: bool
    exit_code: int | None = None
    changed: bool = False
    truncated: bool = False

    @classmethod
    def error(cls, message: str) -> ToolOutcome:
        text = message if message.startswith("ERROR:") else f"ERROR: {message}"
        return cls(text, False)

    def first_line(self, limit: int) -> str:
        """Return a display-safe first line, including for empty output."""
        return next(iter(self.output.splitlines()), "")[:limit]


def _byte_limit_label(value: int) -> str:
    if value >= 1024 * 1024:
        return f"{value // (1024 * 1024)} MiB"
    if value >= 1024:
        return f"{value // 1024} KiB"
    return f"{value} bytes"


@dataclass(frozen=True)
class EditBlock:
    search: str
    replace: str


def safe_join(root: Path, rel: str) -> Path:
    path = (root / rel).resolve()
    resolved_root = root.resolve()
    if resolved_root not in path.parents and path != resolved_root:
        raise ValueError(f"path escapes project root: {rel}")
    return path


def write_file(root: Path, rel: str, content: str) -> ToolOutcome:
    if len(content.encode("utf-8")) > WRITE_MAX_FILE_BYTES:
        return ToolOutcome.error(f"file too large to write: {rel}")
    path = safe_join(root, rel)
    if path.is_file():
        try:
            if path.read_text(encoding="utf-8") == content:
                return ToolOutcome(f"wrote {rel} (no changes)", True)
        except UnicodeDecodeError:
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return ToolOutcome(f"wrote {rel} ({len(content)} chars)", True, changed=True)


def _replace_unique(content: str, search: str, replace: str) -> tuple[str, bool]:
    count = content.count(search)
    if count == 1:
        return content.replace(search, replace, 1), True
    if count == 0 and "\r\n" in content and "\r\n" not in search:
        crlf_search = search.replace("\n", "\r\n")
        crlf_replace = replace.replace("\n", "\r\n")
        crlf_count = content.count(crlf_search)
        if crlf_count == 1:
            return content.replace(crlf_search, crlf_replace, 1), True
    return content, False


def _line_body_without_eol(line: str) -> str:
    return line.removesuffix("\n").removesuffix("\r")


def _line_eol(line: str) -> str:
    if line.endswith("\r\n"):
        return "\r\n"
    if line.endswith("\n"):
        return "\n"
    return ""


def _leading_whitespace(line: str) -> str:
    body = _line_body_without_eol(line)
    return body[: len(body) - len(body.lstrip(" \t"))]


def _has_indented_line(lines: list[str]) -> bool:
    return any(_line_body_without_eol(line).startswith((" ", "\t")) for line in lines)


def _replace_unique_indentation_recovery(
    content: str,
    search: str,
    replace: str,
) -> tuple[str, int]:
    search_lines = search.splitlines(keepends=True)
    replace_lines = replace.splitlines(keepends=True)
    if (
        not search_lines
        or len(search_lines) != len(replace_lines)
        or not _has_indented_line(search_lines)
    ):
        return content, 0

    content_lines = content.splitlines(keepends=True)
    match_starts: list[int] = []
    search_bodies = [
        _line_body_without_eol(line).lstrip(" \t")
        for line in search_lines
    ]
    width = len(search_lines)
    for index in range(0, len(content_lines) - width + 1):
        candidate = content_lines[index : index + width]
        candidate_bodies = [
            _line_body_without_eol(line).lstrip(" \t")
            for line in candidate
        ]
        if candidate_bodies == search_bodies:
            match_starts.append(index)
            if len(match_starts) > 1:
                return content, len(match_starts)

    if len(match_starts) != 1:
        return content, len(match_starts)

    start = match_starts[0]
    candidate = content_lines[start : start + width]
    aligned: list[str] = []
    for candidate_line, replacement_line in zip(candidate, replace_lines, strict=True):
        body = _line_body_without_eol(replacement_line)
        eol = _line_eol(candidate_line)
        if body.strip():
            stripped_body = body.lstrip(" \t")
            aligned.append(
                f"{_leading_whitespace(candidate_line)}"
                f"{stripped_body}{eol}"
            )
        else:
            aligned.append(eol)
    updated_lines = [
        *content_lines[:start],
        *aligned,
        *content_lines[start + width :],
    ]
    return "".join(updated_lines), 1


def _bounded_failure_output(lines: list[str]) -> str:
    max_chars = EDIT_FAILURE_MAX_CHARS - len("ERROR: ")
    rendered: list[str] = []
    size = 0
    for line in lines:
        extra = len(line) + (1 if rendered else 0)
        if size + extra > max_chars:
            note = "Additional failure context omitted by character budget."
            note_extra = len(note) + (1 if rendered else 0)
            if size + note_extra <= max_chars:
                rendered.append(note)
            break
        rendered.append(line)
        size += extra
    return "\n".join(rendered)


def _unique_anchor_position(content: str, value: str, kind: str) -> int | None:
    if kind == "literal":
        first = content.find(value)
        if first < 0 or content.find(value, first + 1) >= 0:
            return None
        return first
    pattern = re.compile(
        rf"(?<![A-Za-z0-9_]){re.escape(value)}(?![A-Za-z0-9_])"
    )
    matches = pattern.finditer(content)
    first = next(matches, None)
    if first is None or next(matches, None) is not None:
        return None
    return first.start()


def _edit_anchor(content: str, search: str) -> tuple[str, str, int] | None:
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()
    for match in EDIT_ANCHOR_RE.finditer(search):
        literal = match.group("literal")
        identifier = match.group("identifier")
        value = literal or identifier or ""
        if (
            not value
            or len(value) > EDIT_FAILURE_MAX_LINE_CHARS
            or value in seen
            or value.lower() in EDIT_ANCHOR_STOPWORDS
        ):
            continue
        seen.add(value)
        kind = "literal" if literal is not None else "identifier"
        candidates.append((value, kind))
    candidates.sort(key=lambda item: -len(item[0]))
    for value, kind in candidates[:EDIT_FAILURE_MAX_ANCHOR_CANDIDATES]:
        position = _unique_anchor_position(content, value, kind)
        if position is not None:
            return value, kind, position
    return None


def _render_edit_failure_context(content: str, search: str) -> str:
    anchor = _edit_anchor(content, search)
    if anchor is None:
        return ""
    value, kind, position = anchor
    lines = content.splitlines()
    center = content.count("\n", 0, position)
    radius = EDIT_FAILURE_MAX_LINES // 2
    start = max(0, center - radius)
    end = min(len(lines), start + EDIT_FAILURE_MAX_LINES)
    start = max(0, end - EDIT_FAILURE_MAX_LINES)
    rendered = [
        f'Current bounded context near {kind} "{value}"',
        '(navigation only; copy exact complete code after "|"):',
    ]
    for index in range(start, end):
        line = lines[index]
        if len(line) > EDIT_FAILURE_MAX_LINE_CHARS:
            rendered.append(
                f"Line {index + 1} omitted because it exceeds "
                f"{EDIT_FAILURE_MAX_LINE_CHARS} characters; use read_file "
                f"offset={index + 1} limit=1."
            )
            continue
        rendered.append(f"{index + 1:>4} | {line}")
    return _bounded_failure_output(rendered)


def _first_match_start_lines(
    content: str,
    search: str,
    limit: int = EDIT_FAILURE_MAX_MATCHES,
) -> list[int]:
    if not search:
        return []
    lines: list[int] = []
    offset = 0
    newline_offset = 0
    line_number = 1
    while len(lines) < limit:
        position = content.find(search, offset)
        if position < 0:
            break
        line_number += content.count("\n", newline_offset, position)
        lines.append(line_number)
        newline_offset = position
        offset = position + len(search)
    return lines


def _render_multiple_match_context(
    content: str,
    search: str,
    match_count: int,
) -> str:
    positions = _first_match_start_lines(content, search)
    if not positions:
        return ""
    lines = ["Exact matches start at lines: " + ", ".join(map(str, positions)) + "."]
    if match_count > len(positions):
        lines.append("Additional matches omitted.")
    lines.append("Add surrounding lines to old_string so it matches one location uniquely.")
    return _bounded_failure_output(lines)


def _replacement_failure_note(index: int, count: int) -> str:
    if count <= 1:
        return ""
    return f"Replacement {index} of {count} failed. No replacements were written."


def _search_not_found(
    rel: str,
    *,
    original_content: str,
    search: str,
    replacement_index: int,
    replacement_count: int,
) -> ToolOutcome:
    lines = [f"SEARCH text not found in {rel}; exact replacement was not applied."]
    note = _replacement_failure_note(replacement_index, replacement_count)
    if note:
        lines.append(note)
    context = _render_edit_failure_context(original_content, search)
    if context:
        lines.append("")
        lines.extend(context.splitlines())
    else:
        lines.append(
            "old_string must match exact file text including indentation and "
            "whitespace. Use read_file and copy exact complete lines."
        )
    return ToolOutcome.error(_bounded_failure_output(lines))


def _multiple_matches(
    rel: str,
    match_count: int,
    *,
    original_content: str,
    search: str,
    replacement_index: int,
    replacement_count: int,
) -> ToolOutcome:
    lines = [
        f"SEARCH text matched {match_count} times in {rel}; replacement was not applied."
    ]
    note = _replacement_failure_note(replacement_index, replacement_count)
    if note:
        lines.append(note)
    original_search = search
    original_count = original_content.count(original_search)
    if original_count == 0 and "\r\n" in original_content and "\r\n" not in search:
        original_search = search.replace("\n", "\r\n")
        original_count = original_content.count(original_search)
    context = (
        _render_multiple_match_context(original_content, original_search, match_count)
        if original_count == match_count
        else ""
    )
    if context:
        lines.append("")
        lines.extend(context.splitlines())
    else:
        lines.append("Add surrounding lines to old_string so it matches one location uniquely.")
    return ToolOutcome.error(_bounded_failure_output(lines))


def _python_syntax_regression_hint(rel: str, before: str, after: str) -> str:
    if Path(rel).suffix.lower() != ".py":
        return ""
    if max(len(before), len(after)) > PYTHON_SYNTAX_HINT_MAX_CHARS:
        return ""

    parse_failures = (
        SyntaxError,
        ValueError,
        TypeError,
        MemoryError,
        RecursionError,
        OverflowError,
    )
    try:
        ast.parse(before, filename=rel)
    except parse_failures:
        return ""

    try:
        ast.parse(after, filename=rel)
    except SyntaxError as exc:
        message = " ".join(str(exc.msg or "invalid syntax").split())
        message = message[:PYTHON_SYNTAX_HINT_MAX_MESSAGE_CHARS]
        location = f"line {max(1, int(exc.lineno or 1))}"
        if exc.offset:
            location += f", column {max(1, int(exc.offset))}"
        return (
            f"\nSyntax regression detected in {rel} at {location}: {message}.\n"
            "The edit was applied. Inspect and repair the current file "
            "before completion."
        )
    except (ValueError, TypeError, MemoryError, RecursionError, OverflowError):
        return ""

    return ""


def edit_file(root: Path, rel: str, blocks: list[EditBlock]) -> ToolOutcome:
    if not blocks:
        return ToolOutcome.error("edit requires at least one replacement")
    if len(blocks) > MAX_REPLACEMENTS:
        return ToolOutcome.error(
            f"edit supports at most {MAX_REPLACEMENTS} replacements"
        )
    if any(not block.search for block in blocks):
        return ToolOutcome.error("edit SEARCH text cannot be empty")

    path = safe_join(root, rel)
    if not path.is_file():
        return ToolOutcome.error(f"not a file: {rel}")
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ToolOutcome.error(f"not utf-8 text: {rel}")

    updated = content
    indentation_recovered = False
    for index, block in enumerate(blocks, start=1):
        exact_count = updated.count(block.search)
        crlf_count = 0
        if exact_count == 0 and "\r\n" in updated and "\r\n" not in block.search:
            crlf_count = updated.count(block.search.replace("\n", "\r\n"))
        recovered_updated = updated
        recovered_count = 0
        if exact_count == 0 and crlf_count == 0:
            recovered_updated, recovered_count = _replace_unique_indentation_recovery(
                updated,
                block.search,
                block.replace,
            )
        total = exact_count or crlf_count or recovered_count
        if total == 0:
            return _search_not_found(
                rel,
                original_content=content,
                search=block.search,
                replacement_index=index,
                replacement_count=len(blocks),
            )
        if total > 1:
            return _multiple_matches(
                rel,
                total,
                original_content=content,
                search=block.search,
                replacement_index=index,
                replacement_count=len(blocks),
            )
        if recovered_count == 1:
            updated = recovered_updated
            indentation_recovered = True
        else:
            updated, replaced = _replace_unique(updated, block.search, block.replace)
            if not replaced:
                return _search_not_found(
                    rel,
                    original_content=content,
                    search=block.search,
                    replacement_index=index,
                    replacement_count=len(blocks),
                )

    if updated == content:
        return ToolOutcome(f"edited {rel} (no changes)", True)
    if len(updated.encode("utf-8")) > WRITE_MAX_FILE_BYTES:
        return ToolOutcome.error(f"file too large to write: {rel}")
    syntax_hint = _python_syntax_regression_hint(rel, content, updated)
    path.write_text(updated, encoding="utf-8")
    count = len(blocks)
    label = "replacement" if count == 1 else "replacements"
    note = "; indentation recovered" if indentation_recovered else ""
    return ToolOutcome(
        f"edited {rel} ({count} {label}{note}){syntax_hint}",
        True,
        changed=True,
    )


def bounded_positive_int(
    value: object,
    name: str,
    maximum: int | None = None,
) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        parsed = value
    elif isinstance(value, str) and value.strip().isdigit():
        parsed = int(value)
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
    return json.dumps(
        {
            "tool": "read_file",
            "args": {
                "path": rel,
                "offset": offset,
                "limit": limit,
            },
        },
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
    path = safe_join(root, rel)
    if not path.is_file():
        return ToolOutcome.error(f"not a file: {rel}")
    try:
        start_line = bounded_positive_int(offset, "offset")
        line_limit = bounded_positive_int(limit, "limit", READ_MAX_LINES)
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ToolOutcome.error(f"not utf-8 text: {rel}")
    except ValueError as exc:
        return ToolOutcome.error(str(exc))

    lines = text.splitlines(keepends=True)
    total = len(lines)
    if total == 0:
        if start_line != 1:
            return ToolOutcome.error(f"offset {start_line} exceeds {rel} total lines 0")
        return ToolOutcome("", True)
    if start_line > total:
        return ToolOutcome.error(
            f"offset {start_line} exceeds {rel} total lines {total}"
        )

    available = lines[start_line - 1 : start_line - 1 + line_limit]
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
        return ToolOutcome(
            _append_page_metadata(preview, metadata),
            True,
            truncated=True,
        )

    selected: list[str] = []
    chars = 0
    for line in available:
        if selected and chars + len(line) > READ_MAX_CHARS:
            break
        selected.append(line)
        chars += len(line)

    content = "".join(selected)
    end_line = start_line + len(selected) - 1
    partial = start_line > 1 or end_line < total
    if not partial:
        return ToolOutcome(content, True)
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
    return ToolOutcome(
        _append_page_metadata(content, metadata),
        True,
        truncated=True,
    )


def list_directory(root: Path, rel: str) -> ToolOutcome:
    path = safe_join(root, rel)
    if not path.is_dir():
        return ToolOutcome.error(f"not a directory: {rel}")
    lines: list[str] = []
    for entry in sorted(path.iterdir()):
        if entry.name.startswith("."):
            continue
        if entry.is_dir():
            lines.append(f"{entry.name}/")
            for sub in sorted(entry.iterdir())[:50]:
                if sub.name.startswith("."):
                    continue
                tag = "/" if sub.is_dir() else ""
                lines.append(f"  {sub.name}{tag}")
        else:
            lines.append(entry.name)
    return ToolOutcome("\n".join(lines) if lines else "(empty)", True)


def search_files(
    root: Path,
    rel: str,
    query: str,
    *,
    max_results: int = SEARCH_MAX_RESULTS,
) -> ToolOutcome:
    query = query.strip()
    if not query:
        return ToolOutcome.error("search query required")
    start = safe_join(root, rel or ".")
    symlink_reason = _raw_path_symlink_reason(root, rel or ".", tool="grep")
    if symlink_reason:
        return ToolOutcome.error(symlink_reason)
    if not start.exists():
        return ToolOutcome.error(f"path not found: {rel}")
    needle = query.lower()
    matches: list[str] = []
    result_limited = False
    bytes_read = 0
    byte_limited = False
    oversized_files = 0
    resolved_root = root.resolve()
    budget = BoundedScanBudget(
        max_files=SEARCH_MAX_SCAN_FILES,
        max_dirs=SEARCH_MAX_SCAN_DIRS,
        max_dir_entries=SEARCH_MAX_DIR_ENTRIES,
    )
    for path in iter_bounded_files(
        start,
        excluded_dirs=SEARCH_EXCLUDED_DIRS,
        budget=budget,
        skip_start_if_excluded=start.resolve() != resolved_root,
    ):
        try:
            size = path.stat().st_size
            if size > SEARCH_MAX_FILE_BYTES:
                oversized_files += 1
                continue
            if bytes_read + size > SEARCH_MAX_SCAN_BYTES:
                byte_limited = True
                break
            bytes_read += size
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            if needle not in line.lower():
                continue
            rel_path = path.relative_to(resolved_root).as_posix()
            clean = line.strip()
            if len(clean) > 240:
                clean = clean[:237] + "..."
            matches.append(f"{rel_path}:{line_no}: {clean}")
            if len(matches) >= max_results:
                result_limited = True
                break
        if result_limited:
            break
    if not matches:
        matches.append("(no literal matches; regex is not supported)")
    if result_limited:
        matches.append(f"... truncated after {max_results} matches")
    if oversized_files:
        matches.append(
            f"... skipped {oversized_files} file(s) larger than "
            f"{_byte_limit_label(SEARCH_MAX_FILE_BYTES)}; omitted files may "
            "contain more matches"
        )
    if byte_limited:
        matches.append(
            f"... search scan stopped at {_byte_limit_label(SEARCH_MAX_SCAN_BYTES)} "
            "read budget; omitted files may contain more matches"
        )
    if budget.limited:
        matches.append(budget.stop_message("search scan"))
    truncated = result_limited or budget.limited or byte_limited or bool(oversized_files)
    return ToolOutcome("\n".join(matches), True, truncated=truncated)


def find_references(root: Path, rel: str, symbol: str) -> ToolOutcome:
    symbol = str(symbol or "").strip()
    if not symbol:
        return ToolOutcome.error("symbol required")
    try:
        start = safe_join(root, rel or ".")
        symlink_reason = _raw_path_symlink_reason(
            root,
            rel or ".",
            tool="find_references",
        )
        if symlink_reason:
            return ToolOutcome.error(symlink_reason)
        if not start.exists():
            return ToolOutcome.error(f"path not found: {rel}")
        scan = find_reference_hints(root, start, symbol)
    except ValueError as exc:
        return ToolOutcome.error(str(exc))
    return ToolOutcome(scan.output, True, truncated=scan.truncated)


def _raw_path_symlink_reason(root: Path, rel: str, *, tool: str) -> str:
    raw = Path(str(rel or "."))
    try:
        parts = raw.relative_to(root).parts if raw.is_absolute() else raw.parts
    except ValueError:
        return ""
    current = root
    for part in parts:
        if part in {"", "."}:
            continue
        current = current / part
        try:
            if current.is_symlink():
                return f"symlink paths are not supported for {tool}: {rel}"
        except OSError:
            return ""
    return ""


def _command_has_forbidden_tokens(argv: list[str]) -> bool:
    return any(token in arg for arg in argv for token in RUN_FORBIDDEN_TOKENS)


def _is_allowed_run_command(argv: list[str]) -> bool:
    if not argv:
        return False
    exe = Path(argv[0]).name.lower()
    if exe in {"python", "python.exe", "py", "py.exe"}:
        args = argv[1:]
        while args and args[0] in RUN_ALLOWED_PYTHON_FLAGS:
            args = args[1:]
        if len(args) >= 2 and args[0] == "-m" and args[1] in {
            "unittest",
            "pytest",
            "py_compile",
        }:
            return True
        return bool(args and args[0].endswith(".py"))
    if exe in {"pytest", "pytest.exe"}:
        return True
    if exe in {"npm", "npm.cmd", "npm.exe"}:
        return len(argv) >= 2 and (
            argv[1] in RUN_ALLOWED_NPM_SCRIPTS
            or (len(argv) >= 3 and argv[1] == "run" and argv[2] in RUN_ALLOWED_NPM_SCRIPTS)
        )
    if exe in {"pnpm", "pnpm.cmd", "pnpm.exe", "yarn", "yarn.cmd", "yarn.exe"}:
        return len(argv) >= 2 and (
            argv[1] in RUN_ALLOWED_NPM_SCRIPTS
            or (len(argv) >= 3 and argv[1] == "run" and argv[2] in RUN_ALLOWED_NPM_SCRIPTS)
        )
    if exe in {"go", "go.exe"}:
        return len(argv) >= 2 and argv[1] in {"test", "build", "vet"}
    if exe in {"cargo", "cargo.exe"}:
        return len(argv) >= 2 and argv[1] in {"test", "build", "check"}
    if exe in {"dotnet", "dotnet.exe"}:
        return len(argv) >= 2 and argv[1] in {"test", "build"}
    return False


def run_command(root: Path, rel: str, command: str) -> ToolOutcome:
    command = command.strip()
    if not command:
        return ToolOutcome.error("command required")
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return ToolOutcome.error(f"invalid command: {exc}")
    if _command_has_forbidden_tokens(argv) or not _is_allowed_run_command(argv):
        return ToolOutcome.error(f"command not allowed: {command}")
    cwd = safe_join(root, rel or ".")
    if not cwd.is_dir():
        return ToolOutcome.error(f"not a directory: {rel}")
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        proc = cancellation.run_process(
            argv,
            cwd=cwd,
            env=env,
            timeout=RUN_TIMEOUT_SECONDS,
            shell=False,
        )
    except FileNotFoundError:
        return ToolOutcome.error(f"command not found: {argv[0]}")
    except subprocess.TimeoutExpired:
        return ToolOutcome.error(
            f"command timed out after {RUN_TIMEOUT_SECONDS}s: {command}"
        )

    output_parts = []
    if proc.stdout:
        output_parts.append(proc.stdout.rstrip())
    if proc.stderr:
        output_parts.append("[stderr]\n" + proc.stderr.rstrip())
    output = "\n\n".join(output_parts) or "(no output)"
    output = prune_dependency_stack_frames(output, root)
    output, truncated = clip_middle(output, RUN_OUTPUT_LIMIT)
    display = f"exit {proc.returncode}: {command}\n{output}"
    return ToolOutcome(
        display,
        proc.returncode == 0,
        exit_code=proc.returncode,
        truncated=truncated,
    )
