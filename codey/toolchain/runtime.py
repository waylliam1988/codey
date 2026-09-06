"""Local project tools with structured outcomes."""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from codey.runtime import cancellation
from codey.storage.atomic_io import write_text_atomic
from codey.policies.action import (
    ActionSubject,
    DECISION_DENY,
    evaluate_action,
)
from codey.policies.run_command_semantics import (
    RunCommandPolicyError,
    canonical_run_command,
    command_has_forbidden_tokens,
    is_allowed_run_command,
    is_suite_run_command,
    strip_python_flags,
)
from codey.workspace.bounded_scan import (
    DEFAULT_MAX_DIR_ENTRIES,
    DEFAULT_MAX_SCAN_DIRS,
    DEFAULT_MAX_SCAN_FILES,
    BoundedScanBudget,
    iter_bounded_files,
)
from codey.runtime.models import (
    json_safe_projection,
    model_text_with_audit_markers,
    normalized_managed_output,
)
from codey.utils.references import find_reference_hints
from codey.reviews.scan_report import render_scan_coverage
from codey.utils.text_budget import clip_middle, prune_dependency_stack_frames


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
LIST_MAX_DIR_ENTRIES = DEFAULT_MAX_DIR_ENTRIES
LIST_MAX_SUBDIR_ENTRIES = 50
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
RUN_SUITE_TIMEOUT_SECONDS = 300
RUN_OUTPUT_LIMIT = 24_000
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
    model_text: str
    ok: bool
    canonical: Mapping[str, object] = field(default_factory=dict)
    presentation: Mapping[str, object] = field(default_factory=dict)
    audit: Mapping[str, object] = field(default_factory=dict)
    error_code: str = ""
    exit_code: int | None = None
    changed: bool = False
    truncated: bool = False

    def __post_init__(self) -> None:
        model_text = str(self.model_text or "")
        error_code = str(self.error_code or ("error" if not self.ok else ""))
        presentation = json_safe_projection(self.presentation, label="presentation")
        presentation.setdefault("status", "ok" if self.ok else "error")
        presentation.setdefault("result", _first_model_line(model_text, 200))
        audit = json_safe_projection(self.audit, label="audit")
        if error_code:
            audit["error_code"] = error_code
        if self.exit_code is not None:
            audit["exit_code"] = self.exit_code
        if self.changed:
            audit["changed"] = True
        if self.truncated:
            audit["truncated"] = True
        canonical = json_safe_projection(self.canonical, label="canonical")
        model_text = model_text_with_audit_markers(
            model_text,
            truncated=self.truncated,
            audit=audit,
        )
        object.__setattr__(self, "model_text", model_text)
        object.__setattr__(self, "error_code", error_code)
        object.__setattr__(self, "canonical", canonical)
        object.__setattr__(self, "presentation", presentation)
        object.__setattr__(self, "audit", audit)

    @classmethod
    def error(
        cls,
        message: str,
        *,
        error_code: str = "error",
        audit: Mapping[str, object] | None = None,
    ) -> ToolOutcome:
        text = message if message.startswith("ERROR:") else f"ERROR: {message}"
        audit_payload = dict(audit or {})
        audit_payload["error_code"] = error_code
        return cls(
            text,
            False,
            presentation={"status": "error", "result": _first_model_line(text, 200)},
            audit=audit_payload,
            error_code=error_code,
        )

    def first_model_line(self, limit: int) -> str:
        """Return a display-safe first model-visible line, including empty text."""
        return _first_model_line(self.model_text, limit)

    def presentation_result(self, limit: int) -> str:
        value = self.presentation.get("result") if isinstance(self.presentation, Mapping) else ""
        text = str(value or "") or self.first_model_line(limit)
        return text[:limit]

    def presentation_status(self) -> str:
        value = self.presentation.get("status") if isinstance(self.presentation, Mapping) else ""
        return str(value or "") or ("ok" if self.ok else "error")

    def managed_output(self) -> Mapping[str, object]:
        value = self.audit.get("managed_output") if isinstance(self.audit, Mapping) else None
        return normalized_managed_output(value)


def _policy_error_outcome(decision) -> ToolOutcome:
    message = str(getattr(decision, "display", "") or "action denied by policy")
    text = message if message.startswith("ERROR:") else f"ERROR: {message}"
    return ToolOutcome(
        text,
        False,
        presentation={"status": "error", "result": _first_model_line(text, 200)},
        audit={"error_code": "policy_denied", "policy_decision": decision.to_audit_payload()},
        error_code="policy_denied",
    )


@dataclass(frozen=True)
class RunCommandRawResult:
    command: str
    output: str
    ok: bool
    exit_code: int
    started_at: str = ""
    finished_at: str = ""
    duration_ms: int | None = None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _first_model_line(text: object, limit: int) -> str:
    return next(iter(str(text or "").splitlines()), "")[:limit]


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
    """Resolve ``rel`` under ``root`` and prevent relative path traversal.

    Threat model: This guard defends against model-generated relative path
    escapes (e.g. ``../../etc/passwd`` or absolute escape strings). It is an
    application-level path validation guard and not an operating-system-level
    capability sandbox (e.g. symlink races / TOCTOU across directory swaps).
    """
    path = (root / rel).resolve()
    resolved_root = root.resolve()
    if resolved_root not in path.parents and path != resolved_root:
        raise ValueError(f"path escapes project root: {rel}")
    return path


def _symlink_path_error(root: Path, rel: str, *, tool: str) -> ToolOutcome | None:
    reason = _raw_path_symlink_reason(root, rel, tool=tool)
    if reason:
        return ToolOutcome.error(reason, error_code="symlink_path")
    return None


def _checked_tool_path(
    root: Path,
    rel: str,
    *,
    tool: str,
) -> tuple[Path | None, ToolOutcome | None]:
    try:
        path = safe_join(root, rel)
    except (OSError, RuntimeError, ValueError) as exc:
        return None, ToolOutcome.error(str(exc), error_code="workspace_escape")
    symlink_error = _symlink_path_error(root, rel, tool=tool)
    if symlink_error is not None:
        return None, symlink_error
    return path, None


def _write_text_after_final_path_check(
    root: Path,
    rel: str,
    content: str,
    *,
    tool: str,
) -> ToolOutcome | None:
    path, error = _checked_tool_path(root, rel, tool=tool)
    if error is not None or path is None:
        return error or ToolOutcome.error("path could not be resolved")
    write_text_atomic(path, content)
    return None


def write_file(root: Path, rel: str, content: str) -> ToolOutcome:
    if len(content.encode("utf-8")) > WRITE_MAX_FILE_BYTES:
        return ToolOutcome.error(f"file too large to write: {rel}")
    path, error = _checked_tool_path(root, rel, tool="write_file")
    if error is not None or path is None:
        return error or ToolOutcome.error("path could not be resolved")
    before = ""
    if path.is_file():
        try:
            before = path.read_text(encoding="utf-8")
            if before == content:
                return ToolOutcome(f"wrote {rel} (no changes)", True)
        except UnicodeDecodeError:
            pass
    write_error = _write_text_after_final_path_check(
        root,
        rel,
        content,
        tool="write_file",
    )
    if write_error is not None:
        return write_error
    syntax_hint = _python_syntax_regression_hint(rel, before, content)
    return ToolOutcome(
        f"wrote {rel} ({len(content)} chars){syntax_hint}",
        True,
        changed=True,
    )


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

    path, error = _checked_tool_path(root, rel, tool="edit")
    if error is not None or path is None:
        return error or ToolOutcome.error("path could not be resolved")
    if not path.is_file():
        return ToolOutcome.error(f"not a file: {rel}")
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ToolOutcome.error(f"not utf-8 text: {rel}")

    updated = content
    for index, block in enumerate(blocks, start=1):
        exact_count = updated.count(block.search)
        crlf_count = 0
        if exact_count == 0 and "\r\n" in updated and "\r\n" not in block.search:
            crlf_count = updated.count(block.search.replace("\n", "\r\n"))
        total = exact_count or crlf_count
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
    write_error = _write_text_after_final_path_check(root, rel, updated, tool="edit")
    if write_error is not None:
        return write_error
    count = len(blocks)
    label = "replacement" if count == 1 else "replacements"
    return ToolOutcome(
        f"edited {rel} ({count} {label}){syntax_hint}",
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
    cancellation.check()
    path, error = _checked_tool_path(root, rel, tool="read_file")
    if error is not None or path is None:
        return error or ToolOutcome.error("path could not be resolved")
    if not path.is_file():
        return ToolOutcome.error(f"not a file: {rel}")
    try:
        start_line = bounded_positive_int(offset, "offset")
        line_limit = bounded_positive_int(limit, "limit", READ_MAX_LINES)
        text = path.read_text(encoding="utf-8")
        cancellation.check()
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
        cancellation.check()
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


def _bounded_directory_entries(path: Path, max_entries: int) -> tuple[list[Path], bool]:
    entries: list[Path] = []
    limited = False
    for index, entry in enumerate(path.iterdir()):
        cancellation.check()
        if index >= max_entries:
            limited = True
            break
        if not entry.name.startswith("."):
            entries.append(entry)
    return sorted(entries), limited


def list_directory(root: Path, rel: str) -> ToolOutcome:
    cancellation.check()
    path, error = _checked_tool_path(root, rel, tool="list_dir")
    if error is not None or path is None:
        return error or ToolOutcome.error("path could not be resolved")
    if not path.is_dir():
        return ToolOutcome.error(f"not a directory: {rel}")
    lines: list[str] = []
    entries, entry_limited = _bounded_directory_entries(path, LIST_MAX_DIR_ENTRIES)
    for entry in entries:
        cancellation.check()
        if entry.is_dir():
            lines.append(f"{entry.name}/")
            sub_entries, sub_limited = _bounded_directory_entries(entry, LIST_MAX_SUBDIR_ENTRIES)
            for sub in sub_entries:
                cancellation.check()
                tag = "/" if sub.is_dir() else ""
                lines.append(f"  {sub.name}{tag}")
            if sub_limited:
                lines.append(
                    f"  ... list_dir stopped after {LIST_MAX_SUBDIR_ENTRIES} directory entries"
                )
        else:
            lines.append(entry.name)
    if entry_limited:
        lines.append(f"... list_dir stopped after {LIST_MAX_DIR_ENTRIES} directory entries")
    return ToolOutcome("\n".join(lines) if lines else "(empty)", True)


def search_files(
    root: Path,
    rel: str,
    query: str,
    *,
    max_results: int = SEARCH_MAX_RESULTS,
) -> ToolOutcome:
    cancellation.check()
    query = query.strip()
    if not query:
        return ToolOutcome.error("search query required")
    start, error = _checked_tool_path(root, rel or ".", tool="grep")
    if error is not None or start is None:
        return error or ToolOutcome.error("path could not be resolved")
    if not start.exists():
        return ToolOutcome.error(f"path not found: {rel}")
    needle = query.lower()
    matches: list[str] = []
    result_limited = False
    bytes_read = 0
    byte_limited = False
    oversized_files = 0
    unreadable_files = 0
    decode_failed_files = 0
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
        cancellation.check()
        try:
            size = path.stat().st_size
        except OSError:
            unreadable_files += 1
            continue
        if size > SEARCH_MAX_FILE_BYTES:
            oversized_files += 1
            continue
        if bytes_read + size > SEARCH_MAX_SCAN_BYTES:
            byte_limited = True
            break
        bytes_read += size
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            decode_failed_files += 1
            continue
        except OSError:
            unreadable_files += 1
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            if line_no == 1 or line_no % 200 == 0:
                cancellation.check()
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
        matches.append(
            f"... truncated after {max_results} matches; narrow the query or pass a "
            "subdirectory in path to see the rest"
        )
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
    if unreadable_files or decode_failed_files:
        matches.append("Scan coverage:")
        if unreadable_files:
            plural = "file" if unreadable_files == 1 else "files"
            matches.append(
                f"- search could not read metadata or contents for "
                f"{unreadable_files} {plural}; omitted files may contain more matches"
            )
        if decode_failed_files:
            plural = "file" if decode_failed_files == 1 else "files"
            matches.append(
                f"- search skipped {decode_failed_files} non-UTF-8 {plural}; "
                "omitted files may contain more matches"
            )
    truncated = (
        result_limited
        or budget.limited
        or byte_limited
        or bool(oversized_files)
        or bool(unreadable_files)
        or bool(decode_failed_files)
    )
    return ToolOutcome("\n".join(matches), True, truncated=truncated)


def find_references(root: Path, rel: str, symbol: str) -> ToolOutcome:
    symbol = str(symbol or "").strip()
    if not symbol:
        return ToolOutcome.error("symbol required")
    start, error = _checked_tool_path(root, rel or ".", tool="find_references")
    if error is not None or start is None:
        return error or ToolOutcome.error("path could not be resolved")
    if not start.exists():
        return ToolOutcome.error(f"path not found: {rel}")
    try:
        scan = find_reference_hints(root, start, symbol)
    except ValueError as exc:
        return ToolOutcome.error(str(exc))
    coverage = render_scan_coverage(scan.report) if scan.report is not None else ""
    output = f"{scan.output}\n{coverage}" if coverage else scan.output
    incomplete = bool(scan.report and scan.report.incomplete)
    return ToolOutcome(output, True, truncated=scan.truncated or incomplete)


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
    return command_has_forbidden_tokens(argv)


def _strip_python_flags(argv: list[str]) -> list[str]:
    return strip_python_flags(argv)


def _is_allowed_run_command(argv: list[str]) -> bool:
    return is_allowed_run_command(argv)


def _is_suite_run_command(argv: list[str]) -> bool:
    """Recognize verification suites that legitimately need a longer budget."""
    return is_suite_run_command(argv)


def run_command_raw(
    root: Path,
    rel: str,
    command: str,
    *,
    permission_profile: str,
    phase: str = "tool_runtime",
) -> RunCommandRawResult | ToolOutcome:
    command = command.strip()
    decision = evaluate_action(ActionSubject(
        kind="run_command",
        phase=phase,
        permission_profile=permission_profile,
        project=str(root),
        path=rel or ".",
        command=command,
        tool_name="run",
    ))
    if decision.decision == DECISION_DENY:
        return _policy_error_outcome(decision)
    try:
        canonical = canonical_run_command(root, rel or ".", command)
    except RunCommandPolicyError as exc:
        # Fail closed: an untokenizable command never executes.
        return ToolOutcome.error(exc.display, error_code=exc.reason_code)
    argv = list(canonical.argv)
    cwd = canonical.cwd
    if not cwd.is_dir():
        return ToolOutcome.error(f"not a directory: {rel}")
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    timeout = RUN_SUITE_TIMEOUT_SECONDS if _is_suite_run_command(argv) else RUN_TIMEOUT_SECONDS
    started_at = _utc_now_iso()
    started_monotonic = time.monotonic()
    try:
        proc = cancellation.run_process(
            argv,
            cwd=cwd,
            env=env,
            timeout=timeout,
            shell=False,
        )
    except FileNotFoundError:
        return ToolOutcome.error(f"command not found: {argv[0]}")
    except subprocess.TimeoutExpired:
        finished_at = _utc_now_iso()
        duration_ms = max(0, int((time.monotonic() - started_monotonic) * 1000))
        message = (
            f"command timed out after {timeout}s (this is a timeout, not a test "
            f"failure): {command}. Re-run a smaller subset or a single test/file to "
            "verify instead of guessing a fix."
        )
        # The process did launch, so this is an execution fact: timing flows
        # through audit so AnalysisRun projection can record it honestly.
        return ToolOutcome.error(
            message,
            error_code="timeout",
            audit={
                "command_started_at": started_at,
                "command_finished_at": finished_at,
                "command_duration_ms": duration_ms,
            },
        )
    duration_ms = max(0, int((time.monotonic() - started_monotonic) * 1000))

    output_parts = []
    if proc.stdout:
        output_parts.append(proc.stdout.rstrip())
    if proc.stderr:
        output_parts.append("[stderr]\n" + proc.stderr.rstrip())
    output = "\n\n".join(output_parts) or "(no output)"
    return RunCommandRawResult(
        command=command,
        output=output,
        ok=proc.returncode == 0,
        exit_code=proc.returncode,
        started_at=started_at,
        finished_at=_utc_now_iso(),
        duration_ms=duration_ms,
    )


def project_run_command_result(root: Path, raw: RunCommandRawResult) -> ToolOutcome:
    output = prune_dependency_stack_frames(raw.output, root)
    output, truncated = clip_middle(output, RUN_OUTPUT_LIMIT)
    display = f"exit {raw.exit_code}: {raw.command}\n{output}"
    audit: dict[str, object] = {}
    if raw.started_at:
        audit["command_started_at"] = raw.started_at
    if raw.finished_at:
        audit["command_finished_at"] = raw.finished_at
    if raw.duration_ms is not None:
        audit["command_duration_ms"] = max(0, min(int(raw.duration_ms), 10**9))
    return ToolOutcome(
        display,
        raw.ok,
        audit=audit,
        exit_code=raw.exit_code,
        truncated=truncated,
    )


def run_command(
    root: Path,
    rel: str,
    command: str,
    *,
    permission_profile: str,
    phase: str = "tool_runtime",
) -> ToolOutcome:
    raw = run_command_raw(
        root,
        rel,
        command,
        permission_profile=permission_profile,
        phase=phase,
    )
    if isinstance(raw, ToolOutcome):
        return raw
    return project_run_command_result(root, raw)
