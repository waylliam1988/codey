"""Small deterministic project map for first-turn orientation.

This is deliberately not an index, RAG layer, or persisted project database.
It only exposes bounded, local, non-sensitive structure so Writer, advisors,
and Reviewer start from the same basic project facts.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence


MAX_PROJECT_MAP_CHARS = 5_000
MAX_DIRECTORY_ENTRIES = 180
MAX_MANIFEST_BYTES = 64 * 1024
MAX_LISTED_DIRS = 40
MAX_LISTED_FILES = 80
MAX_CANDIDATE_COMMANDS = 12
MAX_SUCCESSFUL_CHECK_LINES = 8

EXCLUDED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".codey",
    ".idea",
    ".vscode",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".next",
    ".turbo",
    "dist",
    "build",
    "target",
    "coverage",
}
SECRET_NAME_PARTS = {
    "apikey",
    "api_key",
    "credential",
    "credentials",
    "passwd",
    "password",
    "private",
    "secret",
    "secrets",
    "token",
    "tokens",
}
SECRET_FILENAMES = {
    ".env",
    ".env.development",
    ".env.local",
    ".env.production",
    ".npmrc",
    ".pypirc",
    "credentials.json",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "service-account.json",
}
SECRET_SUFFIXES = {
    ".env",
    ".key",
    ".pem",
    ".p12",
    ".pfx",
}
LOCK_FILENAMES = {
    "cargo.lock",
    "package-lock.json",
    "pnpm-lock.yaml",
    "poetry.lock",
    "yarn.lock",
}
MANIFEST_NAMES = {
    "package.json",
    "pyproject.toml",
    "pytest.ini",
    "tox.ini",
    "setup.cfg",
    "requirements.txt",
    "Cargo.toml",
    "go.mod",
    "Makefile",
}
SOURCE_DIR_NAMES = {
    "app",
    "apps",
    "bin",
    "cmd",
    "codey",
    "lib",
    "package",
    "packages",
    "scripts",
    "server",
    "src",
    "tests",
    "test",
    "tools",
    "web",
}
DOC_NAMES = {"README.md", "README.zh-CN.md", "DESIGN.md", "BOOTSTRAP_PROOF.md"}


@dataclass(frozen=True)
class ProjectMap:
    directories: tuple[str, ...] = ()
    files: tuple[str, ...] = ()
    manifests: tuple[str, ...] = ()
    source_roots: tuple[str, ...] = ()
    test_roots: tuple[str, ...] = ()
    docs: tuple[str, ...] = ()
    candidate_commands: tuple[str, ...] = ()
    observed_successful_checks: tuple[str, ...] = ()
    truncated: bool = False

    def render(self) -> str:
        lines = ["Project Map (bounded local scan; relative paths only):"]
        _extend_section(lines, "Source/test roots", _dedupe((*self.source_roots, *self.test_roots)))
        _extend_section(lines, "Manifests", self.manifests)
        _extend_section(lines, "Docs", self.docs)
        _extend_section(lines, "Key directories", self.directories)
        _extend_section(lines, "Key files", self.files)
        _extend_section(
            lines,
            "Observed successful checks",
            self.observed_successful_checks,
        )
        _extend_section(
            lines,
            "Candidate commands (inspect before running)",
            self.candidate_commands,
        )
        if self.truncated:
            lines.append("- map truncated: inspect narrower paths as needed")
        text = "\n".join(lines).strip()
        if len(text) <= MAX_PROJECT_MAP_CHARS:
            return text
        return text[:MAX_PROJECT_MAP_CHARS].rstrip() + "\n- map truncated by character budget"


def build_project_map(project: str | Path, verified_facts: str = "") -> ProjectMap:
    root = Path(project).expanduser().resolve()
    dirs: list[str] = []
    files: list[str] = []
    manifests: list[str] = []
    docs: list[str] = []
    source_roots: list[str] = []
    test_roots: list[str] = []
    candidate_commands: list[str] = []
    truncated = False

    entries_seen = 0
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        current, depth = stack.pop()
        if depth > 2:
            continue
        remaining = MAX_DIRECTORY_ENTRIES - entries_seen
        if remaining <= 0:
            truncated = True
            break
        entries, entries_truncated = _bounded_directory_entries(current, remaining)
        if entries_truncated:
            truncated = True
        entries_seen += len(entries)
        for entry in entries:
            rel = _safe_relative(root, entry)
            if not rel or _path_blocked(rel):
                continue
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    name = entry.name
                    if name in EXCLUDED_DIRS:
                        continue
                    is_test_root = name in {"test", "tests"} or name.endswith("_tests")
                    is_source_root = name in SOURCE_DIR_NAMES and not is_test_root
                    if is_source_root:
                        _append_unique(source_roots, rel + "/")
                    if is_test_root:
                        _append_unique(test_roots, rel + "/")
                    if not is_source_root and not is_test_root and len(dirs) < MAX_LISTED_DIRS:
                        dirs.append(rel + "/")
                    stack.append((entry, depth + 1))
                elif entry.is_file():
                    name = entry.name
                    if name in LOCK_FILENAMES:
                        continue
                    if name in MANIFEST_NAMES:
                        _append_unique(manifests, rel)
                        candidate_commands.extend(_manifest_commands(entry, rel))
                    if name in DOC_NAMES:
                        _append_unique(docs, rel)
                    if len(files) < MAX_LISTED_FILES:
                        files.append(rel)
            except OSError:
                continue
        if entries_truncated:
            break

    observed = _observed_successful_checks(verified_facts)
    return ProjectMap(
        directories=tuple(dirs[:MAX_LISTED_DIRS]),
        files=tuple(files[:MAX_LISTED_FILES]),
        manifests=tuple(manifests),
        source_roots=tuple(source_roots),
        test_roots=tuple(test_roots),
        docs=tuple(docs),
        candidate_commands=tuple(_dedupe(candidate_commands)[:MAX_CANDIDATE_COMMANDS]),
        observed_successful_checks=tuple(observed[:MAX_SUCCESSFUL_CHECK_LINES]),
        truncated=truncated,
    )


def render_project_map(project: str | Path, verified_facts: str = "") -> str:
    return build_project_map(project, verified_facts).render()


def _extend_section(lines: list[str], title: str, items: Sequence[str]) -> None:
    if not items:
        return
    lines.append(f"{title}:")
    for item in items:
        lines.append(f"- {item}")


def _bounded_directory_entries(path: Path, remaining: int) -> tuple[list[Path], bool]:
    if remaining <= 0:
        return [], True
    entries: list[Path] = []
    try:
        iterator = path.iterdir()
        for entry in iterator:
            if len(entries) >= remaining:
                return sorted(entries, key=_entry_sort_key), True
            entries.append(entry)
    except OSError:
        return [], False
    return sorted(entries, key=_entry_sort_key), False


def _entry_sort_key(item: Path) -> tuple[bool, str]:
    try:
        is_dir = item.is_dir()
    except OSError:
        is_dir = False
    return (not is_dir, item.name.lower())


def _append_unique(items: list[str], value: str) -> None:
    if value not in items:
        items.append(value)


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        clean = str(item or "").strip()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        result.append(clean)
    return result


def _safe_relative(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return ""


def _path_blocked(rel: str) -> bool:
    normalized = rel.replace("\\", "/").strip("/")
    if not normalized:
        return True
    for part in PurePosixPath(normalized).parts:
        lower = part.lower()
        if lower in EXCLUDED_DIRS or lower in SECRET_FILENAMES:
            return True
        if lower.startswith("."):
            return True
        if any(marker in lower for marker in SECRET_NAME_PARTS):
            return True
        if any(lower.endswith(suffix) for suffix in SECRET_SUFFIXES):
            return True
    return False


def _observed_successful_checks(verified_facts: str) -> list[str]:
    checks: list[str] = []
    for line in (verified_facts or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("- successful check"):
            continue
        _append_unique(checks, stripped.removeprefix("- ").strip())
    return checks


def _manifest_commands(path: Path, rel: str) -> list[str]:
    try:
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            return []
    except OSError:
        return []
    name = path.name
    try:
        if name == "package.json":
            commands = _package_json_commands(path)
        elif name == "pyproject.toml":
            commands = _pyproject_commands(path)
        elif name == "Cargo.toml":
            commands = ["cargo test"]
        elif name == "go.mod":
            commands = ["go test ./..."]
        elif name == "pytest.ini":
            commands = ["python -m pytest"]
        elif name == "tox.ini":
            commands = ["tox"]
        elif name == "Makefile":
            commands = ["make test"]
        elif name == "requirements.txt":
            commands = ["python -m unittest"]
        elif name == "setup.cfg":
            commands = ["python -m pytest"]
        else:
            commands = []
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, tomllib.TOMLDecodeError):
        return []
    return [_scope_command(rel, command) for command in commands]


def _scope_command(rel: str, command: str) -> str:
    parent = PurePosixPath(rel).parent
    if str(parent) == ".":
        return command
    return f"{parent.as_posix()}/: {command}"


def _package_json_commands(path: Path) -> list[str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    scripts = payload.get("scripts") if isinstance(payload, dict) else None
    if not isinstance(scripts, dict):
        return []
    commands: list[str] = []
    for name in ("test", "lint", "typecheck", "build"):
        value = scripts.get(name)
        if isinstance(value, str) and value.strip():
            commands.append(f"npm run {name}" if name != "test" else "npm test")
    return commands


def _pyproject_commands(path: Path) -> list[str]:
    payload = tomllib.loads(path.read_text(encoding="utf-8"))
    tool = payload.get("tool")
    if not isinstance(tool, dict):
        tool = {}
    commands: list[str] = []
    if "pytest" in tool:
        commands.append("python -m pytest")
    if "ruff" in tool:
        commands.append("python -m ruff check .")
    if "mypy" in tool:
        commands.append("python -m mypy .")
    if not commands:
        commands.append("python -m unittest")
    return commands
