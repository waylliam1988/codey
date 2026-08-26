"""Bounded trusted verification discovery and selection."""

from __future__ import annotations

import json
import re
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence

from codey.policies.command_line import split_run_command
from codey.workspace.config import path_matches_ignored_prefix
from codey.toolchain.runtime import _is_allowed_run_command


MAX_SCAN_DIRS = 160
MAX_SCAN_ENTRIES = 2_000
MAX_MANIFEST_BYTES = 256 * 1024
DOC_SUFFIXES = frozenset({".md", ".rst", ".txt"})
EXCLUDED_DIRS = frozenset({
    ".git", ".hg", ".svn", ".venv", "venv", "node_modules",
    "__pycache__", "dist", "build", ".next", "target",
})
NODE_SCRIPTS = ("test", "typecheck", "check", "lint", "build")
MAKE_TARGETS = ("test", "typecheck", "check", "lint", "build")
MAKEFILE_NAMES = ("Makefile", "makefile", "GNUmakefile")
NODE_PACKAGE_MANAGERS = {"npm", "pnpm", "yarn", "bun"}
PYTEST_FULL_SUITE_FLAGS = frozenset({
    "-q",
    "-qq",
    "--quiet",
    "-v",
    "-vv",
    "--verbose",
    "-s",
    "-ra",
    "-rA",
})
NODE_PACKAGE_MANAGER_LOCKFILES = (
    ("pnpm-lock.yaml", "pnpm"),
    ("yarn.lock", "yarn"),
    ("bun.lockb", "bun"),
    ("bun.lock", "bun"),
    ("package-lock.json", "npm"),
)
NODE_SUFFIXES = frozenset({
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".json",
    ".yaml", ".yml", ".lock", ".lockb",
})
PYTHON_SUFFIXES = frozenset({".py", ".pyi", ".toml", ".ini", ".cfg"})
MAKE_SUFFIXES = PYTHON_SUFFIXES | NODE_SUFFIXES | frozenset({
    ".go", ".rs", ".cs", ".java", ".c", ".cc", ".cpp", ".h", ".hpp",
})


@dataclass(frozen=True)
class VerificationCandidate:
    command: str
    cwd: str = "."
    source: str = ""
    previously_passed: bool = False
    source_priority: int = 0


def _safe_cwd(root: Path, value: object) -> str | None:
    text = str(value or ".").strip().replace("\\", "/") or "."
    path = PurePosixPath(text)
    if path.is_absolute() or ".." in path.parts:
        return None
    target = (root / path).resolve()
    if root != target and root not in target.parents:
        return None
    return path.as_posix()


def _allowed_and_available(command: str) -> bool:
    try:
        argv = split_run_command(command)
    except ValueError:
        return False
    return (
        bool(argv)
        and shutil.which(argv[0]) is not None
        and _is_allowed_run_command(argv)
    )


def _candidate(
    root: Path,
    command: object,
    cwd: object,
    source: str,
    *,
    previously_passed: bool = False,
    source_priority: int = 0,
) -> VerificationCandidate | None:
    text = str(command or "").strip()
    safe_cwd = _safe_cwd(root, cwd)
    if not text or safe_cwd is None or not (root / safe_cwd).is_dir():
        return None
    if not _allowed_and_available(text):
        return None
    priority = max(source_priority, 100 if previously_passed else 0)
    return VerificationCandidate(text, safe_cwd, source, previously_passed, priority)


def _bounded_directories(root: Path, ignored_paths: Sequence[str] = ()) -> Iterable[Path]:
    pending = [root]
    seen_dirs = 0
    seen_entries = 0
    while pending and seen_dirs < MAX_SCAN_DIRS:
        current = pending.pop()
        seen_dirs += 1
        yield current
        try:
            children = current.iterdir()
        except OSError:
            continue
        for child in children:
            seen_entries += 1
            if seen_entries > MAX_SCAN_ENTRIES:
                return
            name = child.name
            if name.startswith(".") or name.lower() in EXCLUDED_DIRS:
                continue
            try:
                rel = child.relative_to(root).as_posix()
            except ValueError:
                continue
            if path_matches_ignored_prefix(rel, ignored_paths):
                continue
            try:
                if child.is_dir() and not child.is_symlink():
                    pending.append(child)
            except OSError:
                continue


def _is_manifest_file(path: Path) -> bool:
    try:
        return not path.is_symlink() and path.is_file()
    except OSError:
        return False


def _read_manifest(path: Path) -> str:
    try:
        if not _is_manifest_file(path):
            return ""
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            return ""
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _is_directory(path: Path) -> bool:
    try:
        return not path.is_symlink() and path.is_dir()
    except OSError:
        return False


def _executable_name(value: str) -> str:
    name = Path(value).name.lower()
    for suffix in (".exe", ".cmd"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _package_manager_from_package(package: object) -> str | None:
    if not isinstance(package, dict):
        return None
    value = package.get("packageManager")
    if not isinstance(value, str):
        return None
    name = value.strip().split("@", 1)[0].lower()
    return name if name in NODE_PACKAGE_MANAGERS else None


def _package_manager_from_lockfile(directory: Path) -> str | None:
    for name, manager in NODE_PACKAGE_MANAGER_LOCKFILES:
        if _is_manifest_file(directory / name):
            return manager
    return None


def _nearest_lockfile_package_manager(root: Path, directory: Path) -> str | None:
    current = directory.resolve()
    root = root.resolve()
    while current == root or root in current.parents:
        manager = _package_manager_from_lockfile(current)
        if manager is not None:
            return manager
        if current == root:
            break
        current = current.parent
    return None


def _package_manager(root: Path, directory: Path, package: object) -> str:
    return (
        _package_manager_from_package(package)
        or _package_manager_from_lockfile(directory)
        or _nearest_lockfile_package_manager(root, directory)
        or "npm"
    )


def node_package_manager_for_directory(
    root: str | Path,
    directory: str | Path,
    package: object | None = None,
) -> str:
    """Return the Node package manager implied by package metadata and lockfiles."""

    root_path = Path(root).expanduser().resolve()
    directory_path = Path(directory).expanduser()
    if not directory_path.is_absolute():
        directory_path = root_path / directory_path
    directory_path = directory_path.resolve()

    package_data = package
    if package_data is None:
        package_text = _read_manifest(directory_path / "package.json")
        try:
            package_data = json.loads(package_text) if package_text else {}
        except ValueError:
            package_data = {}
    return _package_manager(root_path, directory_path, package_data)


def _node_script_command(manager: str, script: str) -> str:
    if manager == "bun":
        return f"bun run {script}"
    if script == "test":
        return f"{manager} test"
    return f"{manager} run {script}"


def _node_script_from_argv(argv: list[str]) -> str:
    if len(argv) < 2:
        return ""
    exe = _executable_name(argv[0])
    if exe == "bun":
        if argv[1] == "test":
            return "test"
        return argv[2] if len(argv) >= 3 and argv[1] == "run" else ""
    if exe not in {"npm", "pnpm", "yarn"}:
        return ""
    if argv[1] in NODE_SCRIPTS:
        return argv[1]
    if len(argv) >= 3 and argv[1] == "run":
        return argv[2]
    return ""


def _package_script_exists(root: Path, cwd: str, script: str) -> bool:
    package_text = _read_manifest(root / cwd / "package.json")
    try:
        package = json.loads(package_text) if package_text else {}
    except ValueError:
        return False
    scripts = package.get("scripts") if isinstance(package, dict) else None
    return (
        isinstance(scripts, dict)
        and isinstance(scripts.get(script), str)
        and bool(scripts[script].strip())
    )


def _bun_builtin_test_is_current(root: Path, cwd: str) -> bool:
    directory = root / cwd
    return (
        _is_manifest_file(directory / "bun.lockb")
        or _is_manifest_file(directory / "bun.lock")
        or _is_manifest_file(directory / "package.json")
    )


def _pyproject_tool(pyproject: object, *names: str) -> bool:
    tool = pyproject.get("tool") if isinstance(pyproject, dict) else None
    if not isinstance(tool, dict):
        return False
    current: object = tool
    for name in names:
        if not isinstance(current, dict):
            return False
        current = current.get(name)
    return isinstance(current, dict)


def _make_targets(text: str) -> tuple[str, ...]:
    found: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#") or stripped.startswith("\t"):
            continue
        match = re.match(r"^([A-Za-z0-9_.-]+)\s*:", stripped)
        if not match:
            continue
        remainder = stripped[match.end():].lstrip()
        if remainder.startswith("=") or remainder.startswith(":="):
            continue
        target = match.group(1)
        if target.startswith(".") or "%" in target or target not in MAKE_TARGETS:
            continue
        if target not in seen:
            seen.add(target)
            found.append(target)
    return tuple(found)


def _historical_candidate_is_current(
    root: Path,
    candidate: VerificationCandidate,
) -> bool:
    try:
        argv = split_run_command(candidate.command)
    except ValueError:
        return False
    if not argv:
        return False
    executable = _executable_name(argv[0])
    script = _node_script_from_argv(argv)
    if executable == "bun" and len(argv) >= 2 and argv[1] == "test":
        return _bun_builtin_test_is_current(root, candidate.cwd)
    if executable in NODE_PACKAGE_MANAGERS:
        return bool(script) and _package_script_exists(root, candidate.cwd, script)
    cwd = root / candidate.cwd
    if executable == "cargo":
        return _is_manifest_file(cwd / "Cargo.toml")
    if executable == "go":
        return _is_manifest_file(cwd / "go.mod")
    return True


def discover_verification_candidates(
    project: str | Path,
    verified_commands: Sequence[object] = (),
    configured_commands: Sequence[object] = (),
    ignored_paths: Sequence[str] = (),
) -> tuple[VerificationCandidate, ...]:
    root = Path(project).expanduser().resolve()
    found: list[VerificationCandidate] = []
    for item in verified_commands:
        candidate = _candidate(
            root,
            getattr(item, "command", ""),
            getattr(item, "cwd", "."),
            "previously successful check",
            previously_passed=True,
        )
        if candidate is not None and _historical_candidate_is_current(root, candidate):
            found.append(candidate)
    for item in configured_commands:
        label = str(getattr(item, "label", "") or "").strip()
        source = f"project config: {label}" if label else "project config"
        candidate = _candidate(
            root,
            getattr(item, "command", ""),
            getattr(item, "cwd", "."),
            source,
            source_priority=50,
        )
        if candidate is not None:
            found.append(candidate)
    for directory in _bounded_directories(root, ignored_paths):
        cwd = directory.relative_to(root).as_posix() or "."
        package_text = _read_manifest(directory / "package.json")
        if package_text:
            try:
                package = json.loads(package_text)
            except ValueError:
                package = {}
            scripts = package.get("scripts") if isinstance(package, dict) else None
            if isinstance(scripts, dict):
                manager = _package_manager(root, directory, package)
                for name in NODE_SCRIPTS:
                    if isinstance(scripts.get(name), str) and scripts[name].strip():
                        command = _node_script_command(manager, name)
                        candidate = _candidate(
                            root, command, cwd, f"package.json script {name}"
                        )
                        if candidate is not None:
                            found.append(candidate)
        if _is_manifest_file(directory / "pytest.ini"):
            candidate = _candidate(root, "python -m pytest", cwd, "pytest.ini")
            if candidate is not None:
                found.append(candidate)
        pyproject_text = _read_manifest(directory / "pyproject.toml")
        if pyproject_text:
            try:
                pyproject = tomllib.loads(pyproject_text)
            except tomllib.TOMLDecodeError:
                pyproject = {}
            if _pyproject_tool(pyproject, "pytest") or _pyproject_tool(
                pyproject,
                "pytest",
                "ini_options",
            ):
                candidate = _candidate(
                    root,
                    "python -m pytest",
                    cwd,
                    "tool.pytest",
                )
                if candidate is not None:
                    found.append(candidate)
            if _pyproject_tool(pyproject, "ruff"):
                candidate = _candidate(root, "ruff check .", cwd, "tool.ruff")
                if candidate is not None:
                    found.append(candidate)
            if _pyproject_tool(pyproject, "mypy"):
                candidate = _candidate(root, "mypy .", cwd, "tool.mypy")
                if candidate is not None:
                    found.append(candidate)
        if _is_directory(directory / "tests"):
            candidate = _candidate(
                root,
                "python -m unittest discover",
                cwd,
                "tests directory",
            )
            if candidate is not None:
                found.append(candidate)
        if _is_manifest_file(directory / "ruff.toml") or _is_manifest_file(
            directory / ".ruff.toml"
        ):
            candidate = _candidate(root, "ruff check .", cwd, "ruff config")
            if candidate is not None:
                found.append(candidate)
        if _is_manifest_file(directory / "mypy.ini") or _is_manifest_file(
            directory / ".mypy.ini"
        ):
            candidate = _candidate(root, "mypy .", cwd, "mypy config")
            if candidate is not None:
                found.append(candidate)
        for name in MAKEFILE_NAMES:
            makefile_text = _read_manifest(directory / name)
            if not makefile_text:
                continue
            for target in _make_targets(makefile_text):
                candidate = _candidate(
                    root,
                    f"make {target}",
                    cwd,
                    f"{name} target {target}",
                )
                if candidate is not None:
                    found.append(candidate)
            break
        if _is_manifest_file(directory / "Cargo.toml"):
            candidate = _candidate(root, "cargo test", cwd, "Cargo.toml")
            if candidate is not None:
                found.append(candidate)
        if _is_manifest_file(directory / "go.mod"):
            candidate = _candidate(root, "go test ./...", cwd, "go.mod")
            if candidate is not None:
                found.append(candidate)
    unique: dict[tuple[str, str], VerificationCandidate] = {}
    for item in found:
        key = (item.command, item.cwd)
        prior = unique.get(key)
        if prior is None or _effective_source_priority(item) > _effective_source_priority(prior):
            unique[key] = item
    return tuple(unique.values())


def is_document_path(path: str) -> bool:
    item = PurePosixPath(str(path).replace("\\", "/"))
    name = item.name.upper()
    return (
        item.suffix.lower() in DOC_SUFFIXES
        or name == "LICENSE"
        or name.startswith("CHANGELOG")
    )


def _covers(cwd: str, path: str) -> bool:
    return cwd == "." or PurePosixPath(cwd) in PurePosixPath(path).parents


def _is_make_path(path: PurePosixPath) -> bool:
    return path.name in MAKEFILE_NAMES or path.suffix.lower() == ".mk"


def _command_priority(candidate: VerificationCandidate) -> int:
    try:
        argv = split_run_command(candidate.command)
    except ValueError:
        return 0
    if not argv:
        return 0
    exe = _executable_name(argv[0])
    script = _node_script_from_argv(argv)
    if script == "test":
        return 95
    if script == "typecheck":
        return 80
    if script == "check":
        return 70
    if script == "lint":
        return 60
    if script == "build":
        return 10
    if exe in {"python", "py"}:
        args = argv[1:]
        while args and args[0] == "-B":
            args = args[1:]
        if len(args) >= 2 and args[0] == "-m":
            module = args[1]
            if module == "pytest":
                return 100
            if module == "unittest":
                return 90
            if module == "mypy":
                return 80
            if module == "ruff":
                return 60
    if exe == "pytest":
        return 100
    if exe in {"cargo", "go"} and len(argv) >= 2 and argv[1] == "test":
        return 95
    if exe == "mypy":
        return 80
    if exe == "ruff":
        return 60
    if exe in {"make", "gmake"} and len(argv) >= 2:
        target = argv[1]
        if target == "test":
            return 50
        if target == "typecheck":
            return 45
        if target == "check":
            return 40
        if target == "lint":
            return 35
        if target == "build":
            return 5
    return 0


def _command_family(command: str) -> str:
    try:
        argv = split_run_command(command)
    except ValueError:
        return ""
    if not argv:
        return ""
    exe = _executable_name(argv[0])
    script = _node_script_from_argv(argv)
    if script:
        return f"node:{script}"
    if exe in {"python", "py"}:
        args = argv[1:]
        while args and args[0] == "-B":
            args = args[1:]
        if len(args) >= 2 and args[0] == "-m":
            module = args[1]
            if module in {"pytest", "unittest"}:
                return "python:test"
            if module == "mypy":
                return "python:typecheck"
            if module == "ruff":
                return "python:lint"
            if module == "py_compile":
                return "python:compile"
    if exe == "pytest":
        return "python:test"
    if exe == "mypy":
        return "python:typecheck"
    if exe == "ruff":
        return "python:lint"
    if exe in {"cargo", "go"} and len(argv) >= 2:
        return f"{exe}:{argv[1]}"
    if exe in {"make", "gmake"} and len(argv) >= 2:
        return f"make:{argv[1]}"
    return ""


def _pytest_full_suite_args(args: list[str]) -> bool:
    return all(arg in PYTEST_FULL_SUITE_FLAGS for arg in args)


def _is_full_family_command(command: str) -> bool:
    try:
        argv = split_run_command(command)
    except ValueError:
        return False
    if not argv:
        return False
    exe = _executable_name(argv[0])
    script = _node_script_from_argv(argv)
    if script:
        if exe == "bun":
            return (len(argv) == 2 and argv[1] == "test") or (
                len(argv) == 3 and argv[1] == "run"
            )
        return (len(argv) == 2 and argv[1] in NODE_SCRIPTS) or (
            len(argv) == 3 and argv[1] == "run"
        )
    if exe in {"python", "py"}:
        args = argv[1:]
        while args and args[0] == "-B":
            args = args[1:]
        if len(args) >= 2 and args[0] == "-m":
            module = args[1]
            rest = args[2:]
            if module == "pytest":
                return _pytest_full_suite_args(rest)
            if module == "unittest":
                return rest in ([], ["discover"])
            if module == "mypy":
                return rest in ([], ["."])
            if module == "ruff":
                return rest == ["check", "."]
    if exe == "pytest":
        return _pytest_full_suite_args(argv[1:])
    if exe == "mypy":
        return argv[1:] in ([], ["."])
    if exe == "ruff":
        return argv[1:] == ["check", "."]
    return False


def _compatible(candidate: VerificationCandidate, path: str) -> bool:
    item = PurePosixPath(path)
    suffix = item.suffix.lower()
    try:
        argv = split_run_command(candidate.command)
    except ValueError:
        return False
    if not argv:
        return False
    exe = _executable_name(argv[0])
    if exe in {"python", "py", "pytest", "mypy", "ruff"}:
        return suffix in PYTHON_SUFFIXES
    if exe in NODE_PACKAGE_MANAGERS:
        return suffix in NODE_SUFFIXES
    if exe == "cargo" and len(argv) >= 2 and argv[1] == "test":
        return suffix in {".rs", ".toml"}
    if exe == "go" and len(argv) >= 2 and argv[1] == "test":
        return suffix in {".go", ".mod", ".sum"}
    if exe in {"make", "gmake"}:
        return suffix in MAKE_SUFFIXES or _is_make_path(item)
    return False


def select_verification_candidate(
    candidates: Sequence[VerificationCandidate],
    changed_paths: Sequence[str],
) -> VerificationCandidate | None:
    code_paths = tuple(
        dict.fromkeys(
            str(path).replace("\\", "/")
            for path in changed_paths
            if path and not is_document_path(str(path))
        )
    )
    if not code_paths:
        return None
    scored: list[tuple[int, int, int, VerificationCandidate]] = []
    for item in candidates:
        if not all(_covers(item.cwd, path) and _compatible(item, path) for path in code_paths):
            continue
        depth = 0 if item.cwd == "." else len(PurePosixPath(item.cwd).parts)
        scored.append((depth, _effective_source_priority(item), _command_priority(item), item))
    if not scored:
        return None
    scored.sort(key=lambda row: (row[0], row[1], row[2]), reverse=True)
    best = scored[0]
    if len(scored) > 1 and best[:3] == scored[1][:3]:
        return None
    return best[3]


def _effective_source_priority(candidate: VerificationCandidate) -> int:
    return max(int(candidate.source_priority or 0), 100 if candidate.previously_passed else 0)


def check_covers_changes(
    command: str,
    cwd: str,
    changed_paths: Sequence[str],
) -> bool:
    candidate = VerificationCandidate(command, cwd, "successful run")
    return (
        _allowed_and_available(command)
        and select_verification_candidate((candidate,), changed_paths) is not None
    )


def check_covers_selected_candidate(
    candidate: VerificationCandidate,
    command: str,
    cwd: str,
    changed_paths: Sequence[str],
) -> bool:
    if check_matches_candidate(candidate, command, cwd):
        return True
    return (
        bool(_command_family(candidate.command))
        and _command_family(candidate.command) == _command_family(command)
        and _is_full_family_command(command)
        and check_covers_changes(command, cwd, changed_paths)
    )


def check_matches_candidate(
    candidate: VerificationCandidate,
    command: str,
    cwd: str,
) -> bool:
    normalized_cwd = PurePosixPath(str(cwd or ".").replace("\\", "/")).as_posix()
    return command.strip() == candidate.command and normalized_cwd == candidate.cwd


def verification_candidate_lines(
    candidates: Sequence[VerificationCandidate],
    changed_paths: Sequence[str] = (),
) -> tuple[str, ...]:
    selected = select_verification_candidate(candidates, changed_paths) if changed_paths else None
    ordered = list(candidates)
    if selected is not None:
        ordered = [selected, *(item for item in ordered if item != selected)]
    lines: list[str] = []
    for item in ordered:
        line = _verification_candidate_line(item)
        if not line:
            continue
        if line not in lines:
            lines.append(line)
    return tuple(lines)


def selected_verification_candidate_lines(
    candidates: Sequence[VerificationCandidate],
    changed_paths: Sequence[str],
) -> tuple[str, ...]:
    selected = select_verification_candidate(candidates, changed_paths)
    if selected is None:
        return ()
    line = _verification_candidate_line(selected)
    return (line,) if line else ()


def _verification_candidate_line(item: VerificationCandidate) -> str:
    command = str(item.command or "").replace("\r", " ").replace("\n", " ").strip()
    if not command:
        return ""
    cwd_text = str(item.cwd or ".").replace("\r", " ").replace("\n", " ").strip() or "."
    cwd = PurePosixPath(cwd_text.replace("\\", "/")).as_posix()
    return command if cwd == "." else f"{cwd.rstrip('/')}/: {command}"
