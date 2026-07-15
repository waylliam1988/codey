"""Small deterministic project map for first-turn orientation.

This is deliberately not an index, RAG layer, or persisted project database.
It only exposes bounded, local, non-sensitive structure so Writer, advisors,
and Reviewer start from the same basic project facts.
"""

from __future__ import annotations

import ast
import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence

from codey.bounded_scan import BoundedScanBudget, iter_bounded_files


MAX_PROJECT_MAP_CHARS = 7_000
MAX_DIRECTORY_ENTRIES = 180
MAX_MANIFEST_BYTES = 64 * 1024
MAX_LISTED_DIRS = 40
MAX_LISTED_FILES = 80
MAX_CANDIDATE_COMMANDS = 12
MAX_SUCCESSFUL_CHECK_LINES = 8
MAX_SYMBOL_MAP_CHARS = 2_500
MAX_SYMBOL_DIRECTORIES = 240
MAX_SYMBOL_FILES = 120
MAX_SYMBOL_FILE_BYTES = 256 * 1024
MAX_SYMBOL_TOTAL_BYTES = 2 * 1024 * 1024
MAX_SYMBOLS_PER_FILE = 12
MAX_FOCUSED_SUBTREE_CHARS = 2_000
MAX_FOCUS_SCAN_FILES = 1_000
MAX_FOCUS_SCAN_DIRS = 350
MAX_FOCUS_SOURCE_BYTES = 256 * 1024
MAX_FOCUS_TOTAL_BYTES = 8 * 1024 * 1024
MAX_FOCUS_MODULES = 1
MAX_FOCUS_FILES_PER_MODULE = 8
SOURCE_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx"}

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
    symbol_overview: str = ""
    focused_subtree: str = ""
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
        if self.focused_subtree:
            lines.append(self.focused_subtree)
        if self.symbol_overview:
            lines.append(self.symbol_overview)
        if self.truncated:
            lines.append("- map truncated: inspect narrower paths as needed")
        text = "\n".join(lines).strip()
        if len(text) <= MAX_PROJECT_MAP_CHARS:
            return text
        return text[:MAX_PROJECT_MAP_CHARS].rstrip() + "\n- map truncated by character budget"


@dataclass(frozen=True)
class SymbolSummary:
    path: str
    symbols: tuple[str, ...]
    score: int


@dataclass(frozen=True)
class FocusCandidate:
    path: str
    symbols: tuple[str, ...]
    score: int
    module: str
    is_test: bool


@dataclass(frozen=True)
class FocusModule:
    path: str
    max_score: int
    total_score: int
    top_files: tuple[FocusCandidate, ...]


def build_project_map(
    project: str | Path,
    verified_facts: str = "",
    task: str = "",
) -> ProjectMap:
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
    symbol_overview = build_symbol_overview(root, task) if task.strip() else ""
    focused_subtree = build_focused_subtree_overview(root, task) if task.strip() else ""
    return ProjectMap(
        directories=tuple(dirs[:MAX_LISTED_DIRS]),
        files=tuple(files[:MAX_LISTED_FILES]),
        manifests=tuple(manifests),
        source_roots=tuple(source_roots),
        test_roots=tuple(test_roots),
        docs=tuple(docs),
        candidate_commands=tuple(_dedupe(candidate_commands)[:MAX_CANDIDATE_COMMANDS]),
        observed_successful_checks=tuple(observed[:MAX_SUCCESSFUL_CHECK_LINES]),
        symbol_overview=symbol_overview,
        focused_subtree=focused_subtree,
        truncated=truncated,
    )


def render_project_map(
    project: str | Path,
    verified_facts: str = "",
    task: str = "",
) -> str:
    return build_project_map(project, verified_facts, task).render()


def build_symbol_overview(
    project: str | Path,
    task: str,
    *,
    max_chars: int = MAX_SYMBOL_MAP_CHARS,
) -> str:
    task = (task or "").strip()
    if not task:
        return ""
    root = Path(project).expanduser().resolve()
    summaries: list[SymbolSummary] = []
    for path in _iter_symbol_source_files(root):
        rel = _safe_relative(root, path)
        if not rel:
            continue
        symbols = _symbols_for_file(path)
        if not symbols:
            continue
        summaries.append(SymbolSummary(rel, symbols, _symbol_score(rel, symbols, task)))

    if not summaries:
        return ""
    ordered = sorted(summaries, key=lambda item: (-item.score, item.path))
    lines = ["Symbol overview (bounded navigation hints only; read files before editing):"]
    for item in ordered:
        if item.score <= 0 and len(lines) > 10:
            continue
        block = f"- {item.path}: " + "; ".join(item.symbols)
        if len("\n".join(lines)) + len(block) + 2 > max_chars:
            lines.append("- symbol overview truncated; use grep/read_file for narrower inspection")
            break
        lines.append(block)
    return "\n".join(lines) if len(lines) > 1 else ""


def build_focused_subtree_overview(
    project: str | Path,
    task: str,
    *,
    max_chars: int = MAX_FOCUSED_SUBTREE_CHARS,
) -> str:
    task = (task or "").strip()
    if not task:
        return ""
    root = Path(project).expanduser().resolve()
    candidates, budget = _scan_focus_candidates(root, task)
    if not candidates:
        return ""
    if len(candidates) <= MAX_SYMBOL_FILES and not budget.limited:
        return ""

    modules = _focus_modules(candidates)
    focused = tuple(module for module in modules if module.max_score > 0)[:MAX_FOCUS_MODULES]
    if not focused:
        return ""

    lines = ["Focused subtree (task-scored navigation; read files before editing):"]
    for module in focused:
        _append_budgeted_line(lines, f"- {module.path}", max_chars)
        for candidate in module.top_files[:MAX_FOCUS_FILES_PER_MODULE]:
            symbol_text = "; ".join(candidate.symbols[:4])
            role = "test" if candidate.is_test else "source"
            line = f"  - {candidate.path} [{role}]"
            if symbol_text:
                line += f": {symbol_text}"
            if not _append_budgeted_line(lines, line, max_chars):
                _append_budgeted_line(
                    lines,
                    "  - focused subtree truncated; inspect narrower paths as needed",
                    max_chars,
                )
                return "\n".join(lines)
    if budget.limited:
        _append_budgeted_line(
            lines,
            budget.stop_message("focused subtree scan"),
            max_chars,
        )
    return "\n".join(lines) if len(lines) > 1 else ""


def _append_budgeted_line(lines: list[str], line: str, max_chars: int) -> bool:
    current = "\n".join(lines)
    if len(current) + len(line) + 1 > max_chars:
        return False
    lines.append(line)
    return True


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


def _iter_symbol_source_files(root: Path) -> list[Path]:
    files: list[Path] = []
    total_bytes = 0
    directories_seen = 0
    stack = [root]
    while (
        stack
        and len(files) < MAX_SYMBOL_FILES
        and directories_seen < MAX_SYMBOL_DIRECTORIES
    ):
        current = stack.pop()
        directories_seen += 1
        entries, _truncated = _bounded_directory_entries(current, MAX_DIRECTORY_ENTRIES)
        subdirs: list[Path] = []
        for path in entries:
            rel = _safe_relative(root, path)
            if not rel or _path_blocked(rel):
                continue
            try:
                if path.is_symlink():
                    continue
                if path.is_dir():
                    subdirs.append(path)
                    continue
                if not path.is_file():
                    continue
                if path.suffix.lower() not in SOURCE_SUFFIXES:
                    continue
                size = path.stat().st_size
                if size > MAX_SYMBOL_FILE_BYTES:
                    continue
                if total_bytes + size > MAX_SYMBOL_TOTAL_BYTES:
                    break
                total_bytes += size
            except OSError:
                continue
            files.append(path)
            if len(files) >= MAX_SYMBOL_FILES:
                break
        stack.extend(reversed(subdirs))
    return sorted(files)


def _scan_focus_candidates(
    root: Path,
    task: str,
) -> tuple[list[FocusCandidate], BoundedScanBudget]:
    budget = BoundedScanBudget(
        max_files=MAX_FOCUS_SCAN_FILES,
        max_dirs=MAX_FOCUS_SCAN_DIRS,
        max_dir_entries=1_000,
        max_bytes=MAX_FOCUS_TOTAL_BYTES,
    )
    candidates: list[FocusCandidate] = []
    for path in iter_bounded_files(
        root,
        excluded_dirs=EXCLUDED_DIRS,
        budget=budget,
        allow_dir=lambda item: _focus_dir_allowed(root, item),
        allow_file=lambda item: _focus_source_file_allowed(root, item),
    ):
        rel = _safe_relative(root, path)
        if not rel:
            continue
        symbols = _symbols_for_file(path)
        if not symbols:
            continue
        candidates.append(
            FocusCandidate(
                path=rel,
                symbols=symbols,
                score=_symbol_score(rel, symbols, task),
                module=_focus_root(rel),
                is_test=_is_test_path(rel),
            )
        )
    return candidates, budget


def _focus_dir_allowed(root: Path, path: Path) -> bool:
    rel = _safe_relative(root, path)
    return bool(rel and not _path_blocked(rel))


def _focus_source_file_allowed(root: Path, path: Path) -> bool:
    rel = _safe_relative(root, path)
    if not rel or _path_blocked(rel):
        return False
    if path.suffix.lower() not in SOURCE_SUFFIXES:
        return False
    try:
        return path.stat().st_size <= MAX_FOCUS_SOURCE_BYTES
    except OSError:
        return False


def _focus_root(rel: str) -> str:
    parts = PurePosixPath(rel).parts
    if not parts:
        return "."
    if parts[0] in {"apps", "libs", "packages", "services"} and len(parts) >= 2:
        return f"{parts[0]}/{parts[1]}/"
    if parts[0] in {"src", "test", "tests"}:
        return f"{parts[0]}/"
    return f"{parts[0]}/" if len(parts) > 1 else "."


def _is_test_path(rel: str) -> bool:
    parts = {part.lower() for part in PurePosixPath(rel).parts}
    name = PurePosixPath(rel).name.lower()
    return "tests" in parts or "test" in parts or name.startswith("test_")


def _focus_modules(candidates: Sequence[FocusCandidate]) -> tuple[FocusModule, ...]:
    grouped: dict[str, list[FocusCandidate]] = {}
    for candidate in candidates:
        grouped.setdefault(candidate.module, []).append(candidate)

    modules: list[FocusModule] = []
    for module, module_candidates in grouped.items():
        ordered = sorted(
            (item for item in module_candidates if item.score > 0),
            key=lambda item: (item.is_test, -item.score, item.path),
        )
        if not ordered:
            top_files: tuple[FocusCandidate, ...] = ()
        else:
            top_files = tuple(ordered[:MAX_FOCUS_FILES_PER_MODULE])
        modules.append(
            FocusModule(
                path=module,
                max_score=max(item.score for item in module_candidates),
                total_score=sum(item.score for item in module_candidates),
                top_files=top_files,
            )
        )
    return tuple(
        sorted(
            modules,
            key=lambda item: (-item.max_score, -item.total_score, item.path),
        )
    )


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


def _symbols_for_file(path: Path) -> tuple[str, ...]:
    suffix = path.suffix.lower()
    if suffix == ".py":
        return _python_symbols(path)
    if suffix in {".js", ".jsx", ".ts", ".tsx"}:
        return _js_symbols(path)
    return ()


def _python_symbols(path: Path) -> tuple[str, ...]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return ()
    symbols: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            symbols.append(f"class {node.name}")
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    symbols.append(f"def {node.name}.{child.name}({_python_args(child.args)})")
                    if len(symbols) >= MAX_SYMBOLS_PER_FILE:
                        return tuple(symbols)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append(f"def {node.name}({_python_args(node.args)})")
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.isupper():
                    symbols.append(f"const {target.id}")
        if len(symbols) >= MAX_SYMBOLS_PER_FILE:
            break
    return tuple(symbols[:MAX_SYMBOLS_PER_FILE])


def _python_args(args: ast.arguments) -> str:
    names = [arg.arg for arg in (*args.posonlyargs, *args.args)]
    if args.vararg:
        names.append("*" + args.vararg.arg)
    names.extend(arg.arg for arg in args.kwonlyargs)
    if args.kwarg:
        names.append("**" + args.kwarg.arg)
    return ", ".join(names[:6]) + (", ..." if len(names) > 6 else "")


JS_SYMBOL_RE = re.compile(
    r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(([^)]*)\)"
    r"|^\s*(?:export\s+)?class\s+([A-Za-z_$][\w$]*)"
    r"|^\s*(?:export\s+)?interface\s+([A-Za-z_$][\w$]*)"
    r"|^\s*(?:export\s+)?type\s+([A-Za-z_$][\w$]*)\s*="
    r"|^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=",
    re.MULTILINE,
)


def _js_symbols(path: Path) -> tuple[str, ...]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ()
    symbols: list[str] = []
    for match in JS_SYMBOL_RE.finditer(text):
        func, args, cls, interface, typ, const = match.groups()
        if func:
            parts = [part.strip() for part in args.split(",") if part.strip()]
            arg_names = ", ".join(part.split(":")[0].strip() for part in parts[:6])
            suffix = ", ..." if len(parts) > 6 else ""
            symbols.append(f"function {func}({arg_names}{suffix})")
        elif cls:
            symbols.append(f"class {cls}")
        elif interface:
            symbols.append(f"interface {interface}")
        elif typ:
            symbols.append(f"type {typ}")
        elif const:
            symbols.append(f"const {const}")
        if len(symbols) >= MAX_SYMBOLS_PER_FILE:
            break
    return tuple(symbols)


STOP_TOKENS = {
    "and",
    "change",
    "changes",
    "file",
    "files",
    "first",
    "for",
    "implementation",
    "likely",
    "need",
    "should",
    "the",
    "want",
    "where",
    "which",
    "with",
}


def _tokens(text: str) -> set[str]:
    expanded = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text.replace("_", " "))
    return {
        token.lower()
        for token in re.findall(r"[A-Za-z][A-Za-z0-9]{1,}", expanded)
        if token.lower() not in STOP_TOKENS
    }


def _symbol_score(rel: str, symbols: tuple[str, ...], task: str) -> int:
    task_tokens = _tokens(task)
    path_tokens = _tokens(rel.replace("/", " "))
    basename_tokens = _tokens(Path(rel).stem)
    symbol_tokens = _tokens(" ".join(symbols))
    score = 0
    score += 8 * len(task_tokens & path_tokens)
    score += 6 * len(task_tokens & basename_tokens)
    score += 5 * len(task_tokens & symbol_tokens)
    lower_rel = rel.lower()
    lower_symbols = tuple(symbol.lower() for symbol in symbols)
    for token in task_tokens:
        if len(token) >= 4 and token in lower_rel:
            score += 2
        if len(token) >= 4 and any(token in symbol for symbol in lower_symbols):
            score += 2
    if task_tokens & {"check", "test", "testing", "tests", "verification", "verify"}:
        if lower_rel.startswith("tests/") or "/test_" in lower_rel:
            score += 4
    return score


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
