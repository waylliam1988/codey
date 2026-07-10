"""Local project tools with structured outcomes."""

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from codey import cancellation
from codey.file_outline import OUTLINE_MAX_FILE_BYTES, build_file_outline
from codey.text_budget import clip_middle


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
SEARCH_MAX_FILE_BYTES = 512 * 1024
WRITE_MAX_FILE_BYTES = 512 * 1024
READ_DEFAULT_LINES = 300
READ_MAX_LINES = 600
READ_MAX_CHARS = 16_000
MAX_REPLACEMENTS = 8
RUN_TIMEOUT_SECONDS = 90
RUN_OUTPUT_LIMIT = 24_000
RUN_FORBIDDEN_TOKENS = {"&&", "||", ";", "|", ">", ">>", "<", "$(", "`"}
RUN_ALLOWED_PYTHON_FLAGS = {"-B"}
RUN_ALLOWED_NPM_SCRIPTS = {"test", "build", "lint", "check", "typecheck"}
LONG_LINE_MARKER = "\n[... middle of overlong line omitted; not a complete old_string ...]\n"


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
    for block in blocks:
        exact_count = updated.count(block.search)
        crlf_count = 0
        if exact_count == 0 and "\r\n" in updated and "\r\n" not in block.search:
            crlf_count = updated.count(block.search.replace("\n", "\r\n"))
        total = exact_count or crlf_count
        if total == 0:
            return ToolOutcome.error(f"SEARCH text not found in {rel}")
        if total > 1:
            return ToolOutcome.error(
                f"SEARCH text matched {total} times in {rel}; make it unique"
            )
        updated, replaced = _replace_unique(updated, block.search, block.replace)
        if not replaced:
            return ToolOutcome.error(f"SEARCH text not found in {rel}")

    if updated == content:
        return ToolOutcome(f"edited {rel} (no changes)", True)
    if len(updated.encode("utf-8")) > WRITE_MAX_FILE_BYTES:
        return ToolOutcome.error(f"file too large to write: {rel}")
    path.write_text(updated, encoding="utf-8")
    count = len(blocks)
    label = "replacement" if count == 1 else "replacements"
    return ToolOutcome(f"edited {rel} ({count} {label})", True, changed=True)


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
        next_text = f"; next offset={next_offset}" if next_offset <= total else ""
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
    next_text = f"; next offset={end_line + 1}" if end_line < total else ""
    metadata = (
        f"read_file page: lines {start_line}-{end_line} of {total}{next_text}"
    )
    return ToolOutcome(
        _append_page_metadata(content, metadata),
        True,
        truncated=True,
    )


def outline_file(root: Path, rel: str) -> ToolOutcome:
    path = safe_join(root, rel)
    if not path.is_file():
        return ToolOutcome.error(f"not a file: {rel}")
    try:
        if path.stat().st_size > OUTLINE_MAX_FILE_BYTES:
            return ToolOutcome.error(f"file too large to outline: {rel}")
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ToolOutcome.error(f"not utf-8 text: {rel}")
    except OSError as exc:
        return ToolOutcome.error(str(exc))
    outline = build_file_outline(text, rel)
    return ToolOutcome(
        outline,
        True,
        truncated="outline truncated" in outline,
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


def _searchable_files(start: Path) -> list[Path]:
    if start.is_file():
        return [start]
    files: list[Path] = []
    stack = [start]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir():
                if entry.name in SEARCH_EXCLUDED_DIRS:
                    continue
                stack.append(entry)
            elif entry.is_file():
                files.append(entry)
    return files


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
    if not start.exists():
        return ToolOutcome.error(f"path not found: {rel}")
    needle = query.lower()
    matches: list[str] = []
    truncated = False
    resolved_root = root.resolve()
    for path in _searchable_files(start):
        try:
            if path.stat().st_size > SEARCH_MAX_FILE_BYTES:
                continue
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
                truncated = True
                break
        if truncated:
            break
    if not matches:
        return ToolOutcome("(no matches)", True)
    if truncated:
        matches.append(f"... truncated after {max_results} matches")
    return ToolOutcome("\n".join(matches), True, truncated=truncated)


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
    output, truncated = clip_middle(output, RUN_OUTPUT_LIMIT)
    display = f"exit {proc.returncode}: {command}\n{output}"
    return ToolOutcome(
        display,
        proc.returncode == 0,
        exit_code=proc.returncode,
        truncated=truncated,
    )
