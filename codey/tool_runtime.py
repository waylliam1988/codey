"""Local project tools with structured outcomes."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path


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
RUN_TIMEOUT_SECONDS = 90
RUN_OUTPUT_LIMIT = 24_000
RUN_FORBIDDEN_TOKENS = {"&&", "||", ";", "|", ">", ">>", "<", "$(", "`"}
RUN_ALLOWED_PYTHON_FLAGS = {"-B"}
RUN_ALLOWED_NPM_SCRIPTS = {"test", "build", "lint", "check", "typecheck"}
EDIT_BLOCK_RE = re.compile(
    r"<<<<<<< SEARCH\n(.*?)\n=======\n(.*?)\n>>>>>>> REPLACE",
    re.DOTALL,
)


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


def parse_edit_body(body: str) -> list[EditBlock]:
    matches = list(EDIT_BLOCK_RE.finditer(body.strip()))
    if not matches:
        raise ValueError("edit body must contain SEARCH/REPLACE block")
    leftover = EDIT_BLOCK_RE.sub("", body.strip()).strip()
    if leftover:
        raise ValueError("edit body contains text outside SEARCH/REPLACE blocks")
    blocks: list[EditBlock] = []
    for match in matches:
        search = match.group(1)
        replace = match.group(2)
        if not search:
            raise ValueError("SEARCH section cannot be empty")
        blocks.append(EditBlock(search=search, replace=replace))
    return blocks


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


def edit_file(root: Path, rel: str, body: str) -> ToolOutcome:
    path = safe_join(root, rel)
    if not path.is_file():
        return ToolOutcome.error(f"not a file: {rel}")
    try:
        blocks = parse_edit_body(body)
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ToolOutcome.error(f"not utf-8 text: {rel}")
    except ValueError as exc:
        return ToolOutcome.error(str(exc))

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


def read_file(root: Path, rel: str) -> ToolOutcome:
    path = safe_join(root, rel)
    if not path.is_file():
        return ToolOutcome.error(f"not a file: {rel}")
    try:
        return ToolOutcome(path.read_text(encoding="utf-8"), True)
    except UnicodeDecodeError:
        return ToolOutcome.error(f"not utf-8 text: {rel}")


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
        proc = subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=RUN_TIMEOUT_SECONDS,
            shell=False,
            check=False,
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
    truncated = len(output) > RUN_OUTPUT_LIMIT
    if truncated:
        output = output[:RUN_OUTPUT_LIMIT].rstrip() + "\n\n... output truncated by Codey"
    display = f"exit {proc.returncode}: {command}\n{output}"
    return ToolOutcome(
        display,
        proc.returncode == 0,
        exit_code=proc.returncode,
        truncated=truncated,
    )
