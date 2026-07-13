"""Bounded trusted verification discovery and selection."""

from __future__ import annotations

import json
import shlex
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence

from codey.tool_runtime import _is_allowed_run_command


MAX_SCAN_DIRS = 160
MAX_SCAN_ENTRIES = 2_000
MAX_MANIFEST_BYTES = 256 * 1024
DOC_SUFFIXES = frozenset({".md", ".rst", ".txt"})
EXCLUDED_DIRS = frozenset({
    ".git", ".hg", ".svn", ".venv", "venv", "node_modules",
    "__pycache__", "dist", "build", ".next", "target",
})
NPM_SCRIPTS = ("test", "check", "typecheck", "lint")


@dataclass(frozen=True)
class VerificationCandidate:
    command: str
    cwd: str = "."
    source: str = ""
    previously_passed: bool = False


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
        argv = shlex.split(command)
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
) -> VerificationCandidate | None:
    text = str(command or "").strip()
    safe_cwd = _safe_cwd(root, cwd)
    if not text or safe_cwd is None or not (root / safe_cwd).is_dir():
        return None
    if not _allowed_and_available(text):
        return None
    return VerificationCandidate(text, safe_cwd, source, previously_passed)


def _bounded_directories(root: Path) -> Iterable[Path]:
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


def _historical_candidate_is_current(
    root: Path,
    candidate: VerificationCandidate,
) -> bool:
    try:
        argv = shlex.split(candidate.command)
    except ValueError:
        return False
    if not argv:
        return False
    executable = Path(argv[0]).name.lower()
    cwd = root / candidate.cwd
    if executable in {"npm", "npm.cmd"}:
        script = ""
        if len(argv) >= 2 and argv[1] == "test":
            script = "test"
        elif len(argv) >= 3 and argv[1] == "run":
            script = argv[2]
        if not script:
            return False
        package_text = _read_manifest(cwd / "package.json")
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
    if executable == "cargo":
        return _is_manifest_file(cwd / "Cargo.toml")
    if executable == "go":
        return _is_manifest_file(cwd / "go.mod")
    return True


def discover_verification_candidates(
    project: str | Path,
    verified_commands: Sequence[object] = (),
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
    for directory in _bounded_directories(root):
        cwd = directory.relative_to(root).as_posix() or "."
        package_text = _read_manifest(directory / "package.json")
        if package_text:
            try:
                package = json.loads(package_text)
            except ValueError:
                package = {}
            scripts = package.get("scripts") if isinstance(package, dict) else None
            if isinstance(scripts, dict):
                for name in NPM_SCRIPTS:
                    if isinstance(scripts.get(name), str) and scripts[name].strip():
                        command = "npm test" if name == "test" else f"npm run {name}"
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
            tool = pyproject.get("tool") if isinstance(pyproject, dict) else None
            if isinstance(tool, dict) and isinstance(tool.get("pytest"), dict):
                candidate = _candidate(root, "python -m pytest", cwd, "tool.pytest")
                if candidate is not None:
                    found.append(candidate)
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
        if prior is None or item.previously_passed:
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


def _compatible(candidate: VerificationCandidate, path: str) -> bool:
    suffix = PurePosixPath(path).suffix.lower()
    command = candidate.command.lower()
    if any(marker in command for marker in ("pytest", "unittest", "py_compile")):
        return suffix in {".py", ".pyi", ".toml", ".ini", ".cfg"}
    if command.startswith("npm "):
        return suffix in {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".json"}
    if command == "cargo test":
        return suffix in {".rs", ".toml"}
    if command == "go test ./...":
        return suffix in {".go", ".mod", ".sum"}
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
    scored: list[tuple[int, bool, VerificationCandidate]] = []
    for item in candidates:
        if not all(_covers(item.cwd, path) and _compatible(item, path) for path in code_paths):
            continue
        depth = 0 if item.cwd == "." else len(PurePosixPath(item.cwd).parts)
        scored.append((depth, item.previously_passed, item))
    if not scored:
        return None
    scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
    best = scored[0]
    if len(scored) > 1 and best[:2] == scored[1][:2]:
        return None
    return best[2]


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


def check_matches_candidate(
    candidate: VerificationCandidate,
    command: str,
    cwd: str,
) -> bool:
    normalized_cwd = PurePosixPath(str(cwd or ".").replace("\\", "/")).as_posix()
    return command.strip() == candidate.command and normalized_cwd == candidate.cwd
