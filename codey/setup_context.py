"""Read-only setup facts for permissioned shell continuations."""

from __future__ import annotations

import json
import shutil
import tomllib
from pathlib import Path
from pathlib import PurePosixPath

from codey.bounded_scan import BoundedScanBudget, iter_bounded_files
from codey.project_map import EXCLUDED_DIRS, LOCK_FILENAMES, MANIFEST_NAMES, _path_blocked


MAX_SETUP_CONTEXT_CHARS = 2_200
MAX_SETUP_SCAN_FILES = 300
MAX_SETUP_SCAN_DIRS = 120
MAX_SETUP_LISTED_FILES = 24
MAX_MANIFEST_DETAIL_BYTES = 32 * 1024
MAX_SETUP_NOTES = 12

LOCAL_TOOLS = (
    "git",
    "python",
    "py",
    "node",
    "npm",
    "pnpm",
    "yarn",
    "pip",
    "uv",
    "poetry",
    "cargo",
    "go",
    "winget",
)


def safe_setup_context(project: str | Path) -> str:
    try:
        return render_setup_context(project)
    except Exception:
        return ""


def render_setup_context(project: str | Path) -> str:
    """Render a bounded setup context block without running setup commands."""

    root = Path(project).expanduser().resolve()
    if not root.is_dir():
        return ""

    manifests, lockfiles, truncated, listing_limited = _discover_setup_files(root)
    lines = ["Setup Context (read-only diagnosis; no setup commands were run):"]
    lines.append("Local tools:")
    for name in LOCAL_TOOLS:
        status = "available" if shutil.which(name) else "missing"
        lines.append(f"- {name}: {status}")

    if manifests:
        lines.append("")
        lines.append("Project manifests:")
        for rel in manifests[:MAX_SETUP_LISTED_FILES]:
            detail = _manifest_detail(root / rel)
            suffix = f": {detail}" if detail else ""
            lines.append(f"- {rel}{suffix}")

    if lockfiles:
        lines.append("")
        lines.append("Dependency lockfiles:")
        for rel in lockfiles[:MAX_SETUP_LISTED_FILES]:
            lines.append(f"- {rel}")

    if listing_limited:
        lines.append(
            f"- setup context listed first {MAX_SETUP_LISTED_FILES} manifest "
            "or lockfile entries; more may exist"
        )

    setup_notes = _manifest_setup_notes(manifests, lockfiles)
    if setup_notes:
        lines.append("")
        lines.append("Manifest setup notes:")
        lines.extend(f"- {item}" for item in setup_notes)

    if truncated:
        lines.append("- setup scan truncated; inspect narrower paths if needed")

    text = "\n".join(lines).strip()
    if len(text) <= MAX_SETUP_CONTEXT_CHARS:
        return text
    return text[:MAX_SETUP_CONTEXT_CHARS].rstrip() + "\n- setup context truncated"


def _discover_setup_files(
    root: Path,
) -> tuple[tuple[str, ...], tuple[str, ...], bool, bool]:
    names = MANIFEST_NAMES | LOCK_FILENAMES
    budget = BoundedScanBudget(
        max_files=MAX_SETUP_SCAN_FILES,
        max_dirs=MAX_SETUP_SCAN_DIRS,
    )

    manifests: list[str] = []
    lockfiles: list[str] = []

    def allow_file(path: Path) -> bool:
        rel = _relative(root, path)
        return bool(rel) and path.name in names and not _path_blocked(rel)

    def allow_dir(path: Path) -> bool:
        rel = _relative(root, path)
        return bool(rel) and not _path_blocked(rel)

    for path in iter_bounded_files(
        root,
        excluded_dirs=EXCLUDED_DIRS,
        budget=budget,
        allow_dir=allow_dir,
        allow_file=allow_file,
    ):
        rel = _relative(root, path)
        if not rel:
            continue
        if path.name in LOCK_FILENAMES:
            _append_unique(lockfiles, rel)
        elif path.name in MANIFEST_NAMES:
            _append_unique(manifests, rel)

    listing_limited = (
        len(manifests) > MAX_SETUP_LISTED_FILES
        or len(lockfiles) > MAX_SETUP_LISTED_FILES
    )
    return (
        tuple(manifests[:MAX_SETUP_LISTED_FILES]),
        tuple(lockfiles[:MAX_SETUP_LISTED_FILES]),
        budget.limited,
        listing_limited,
    )


def _manifest_detail(path: Path) -> str:
    name = path.name
    if name == "package.json":
        return _package_detail(path)
    if name == "requirements.txt":
        return "Python requirements"
    if name == "pyproject.toml":
        return _pyproject_detail(path)
    if name == "pytest.ini":
        return "pytest config"
    if name == "tox.ini":
        return "tox config"
    if name == "setup.cfg":
        return "Python package/tool config"
    if name == "Cargo.toml":
        return "Rust package"
    if name == "go.mod":
        return "Go module"
    if name == "Makefile":
        return "make targets may exist"
    return ""


def _package_detail(path: Path) -> str:
    try:
        payload = _read_limited(path)
        package = json.loads(payload) if payload else {}
    except (OSError, UnicodeDecodeError, ValueError):
        return "invalid JSON"
    scripts = package.get("scripts") if isinstance(package, dict) else None
    if not isinstance(scripts, dict):
        return "no scripts detected"
    preferred = ("test", "build", "dev", "start", "lint", "check", "typecheck")
    names = [name for name in preferred if isinstance(scripts.get(name), str)]
    if not names:
        return "scripts present"
    return "scripts " + ", ".join(names[:6])


def _pyproject_detail(path: Path) -> str:
    try:
        payload = _read_limited(path)
        data = tomllib.loads(payload) if payload else {}
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return "invalid TOML"
    details: list[str] = []
    if isinstance(data.get("project"), dict):
        details.append("Python project metadata")
    tool = data.get("tool") if isinstance(data, dict) else None
    if isinstance(tool, dict):
        if isinstance(tool.get("poetry"), dict):
            details.append("poetry config")
        if isinstance(tool.get("pytest"), dict):
            details.append("pytest config")
        if isinstance(tool.get("uv"), dict):
            details.append("uv config")
    return ", ".join(details) if details else "Python project config"


def _manifest_setup_notes(
    manifests: tuple[str, ...],
    lockfiles: tuple[str, ...],
) -> tuple[str, ...]:
    notes: list[str] = []
    lock_names_by_dir = {
        (_parent_dir(rel), PurePosixPath(rel).name)
        for rel in lockfiles
    }

    for rel in manifests:
        name = PurePosixPath(rel).name
        parent = _parent_dir(rel)
        scope = _scope_note(parent)
        if name == "package.json":
            command_family = (
                "npm ci or npm install"
                if (parent, "package-lock.json") in lock_names_by_dir
                else "package install commands"
            )
            notes.append(
                f"{rel}: {command_family} should use {scope}; may download "
                "packages, write dependency folders or lockfiles, and run "
                "package lifecycle scripts."
            )
        elif name == "requirements.txt":
            notes.append(
                f"{rel}: Python dependency install should reference this "
                f"manifest or use {scope}; may download packages."
            )
        elif name == "pyproject.toml":
            notes.append(
                f"{rel}: Python project install commands should use {scope}; "
                "may download packages and update environment-specific files."
            )
        elif name == "Cargo.toml":
            notes.append(
                f"{rel}: Rust build/test commands should use {scope}; may "
                "download crates."
            )
        elif name == "go.mod":
            notes.append(
                f"{rel}: Go module commands should use {scope}; may download "
                "modules."
            )
        if len(notes) >= MAX_SETUP_NOTES:
            break
    if not notes:
        notes.append(
            "Commands outside the run allowlist still require shell approval."
        )
    return tuple(dict.fromkeys(notes))


def _parent_dir(rel: str) -> str:
    parent = PurePosixPath(rel).parent.as_posix()
    return "." if parent == "." else parent


def _scope_note(parent: str) -> str:
    return "the project root" if parent == "." else f"{parent}/ as the working directory"


def _read_limited(path: Path) -> str:
    if path.stat().st_size > MAX_MANIFEST_DETAIL_BYTES:
        return ""
    return path.read_text(encoding="utf-8")


def _relative(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return ""


def _append_unique(items: list[str], value: str) -> None:
    if value not in items:
        items.append(value)
