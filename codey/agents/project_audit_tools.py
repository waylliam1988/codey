"""Project-audit scanning tools for read-only advisors (agents leaf).

Owns path filtering, directory budgets, file search, reference lookup,
and read-only call execution. No provider, model-send, or operations
dependencies: the advisor in operations calls this leaf, then the unified
kernel.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from codey.runtime.core.models import ToolCall
from codey.toolchain.runtime import (
    READ_MAX_CHARS,
    SEARCH_MAX_FILE_BYTES,
    SEARCH_MAX_RESULTS,
    SEARCH_MAX_SCAN_BYTES,
    ToolOutcome,
    read_file,
    safe_join,
)
from codey.utils.references import find_reference_hints
from codey.workspace.bounded_scan import BoundedScanBudget, iter_bounded_files

PROJECT_AUDIT_MAX_FILE_BYTES = 256 * 1024
PROJECT_AUDIT_MAX_SCAN_FILES = 1_000
PROJECT_AUDIT_MAX_SCAN_DIRS = 250
PROJECT_AUDIT_MAX_DIR_ENTRIES = 1_000
AUDIT_EXCLUDED_DIRS = {
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
AUDIT_SECRET_NAME_PARTS = {
    "secret",
    "secrets",
    "credential",
    "credentials",
    "token",
    "tokens",
    "password",
    "passwd",
    "private",
    "apikey",
    "api_key",
    "auth",
}
AUDIT_SECRET_FILENAMES = {
    ".env",
    ".env.local",
    ".env.development",
    ".env.production",
    ".npmrc",
    ".pypirc",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "credentials.json",
    "service-account.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "cargo.lock",
    "poetry.lock",
}
AUDIT_SECRET_SUFFIXES = {
    ".env",
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".crt",
    ".cer",
    ".der",
    ".lock",
}
AUDIT_BINARY_SUFFIXES = {
    ".7z",
    ".bin",
    ".bmp",
    ".class",
    ".db",
    ".dll",
    ".dylib",
    ".exe",
    ".gif",
    ".gz",
    ".ico",
    ".jar",
    ".jpeg",
    ".jpg",
    ".pdf",
    ".png",
    ".pyc",
    ".sqlite",
    ".so",
    ".tar",
    ".webp",
    ".zip",
}

def _audit_path_block_reason(rel: str) -> str:
    normalized = (rel or ".").replace("\\", "/").strip("/")
    if normalized in {"", "."}:
        return ""
    parts = [part for part in normalized.split("/") if part]
    for part in parts:
        lower = part.lower()
        if lower in AUDIT_EXCLUDED_DIRS:
            return "excluded directories are not shared with project audit advisors"
        if lower.startswith("."):
            return "hidden dotfiles are not shared with project audit advisors"
        if lower in AUDIT_SECRET_FILENAMES:
            return "sensitive or lock files are not shared with project audit advisors"
        if any(marker in lower for marker in AUDIT_SECRET_NAME_PARTS):
            return "secret-like paths are not shared with project audit advisors"
        if any(lower.endswith(suffix) for suffix in AUDIT_SECRET_SUFFIXES):
            return "key, certificate, and lock files are not shared with project audit advisors"
        if any(lower.endswith(suffix) for suffix in AUDIT_BINARY_SUFFIXES):
            return "binary files are not shared with project audit advisors"
    return ""


def _audit_raw_symlink_reason(root: Path, rel: str) -> str:
    normalized = (rel or ".").replace("\\", "/").strip("/")
    if normalized in {"", "."}:
        return ""
    current = root
    for part in (part for part in normalized.split("/") if part and part != "."):
        current = current / part
        try:
            if current.is_symlink():
                return "symlinks are not shared with project audit advisors"
        except OSError as exc:
            return str(exc)
    return ""


def _audit_file_allowed(path: Path, root: Path) -> tuple[bool, str]:
    try:
        rel = path.relative_to(root).as_posix()
    except ValueError:
        return False, "path escapes project root"
    reason = _audit_path_block_reason(rel)
    if reason:
        return False, reason
    try:
        if path.is_symlink():
            return False, "symlinks are not shared with project audit advisors"
        if not path.is_file():
            return False, "not a file"
        if path.stat().st_size > PROJECT_AUDIT_MAX_FILE_BYTES:
            return False, "file too large for project audit advisors"
    except OSError as exc:
        return False, str(exc)
    return True, ""


def _audit_scannable_file_allowed(path: Path, root: Path) -> bool:
    try:
        rel = path.relative_to(root).as_posix()
    except ValueError:
        return False
    if _audit_path_block_reason(rel):
        return False
    try:
        return path.is_file() and not path.is_symlink()
    except OSError:
        return False


def _audit_visible_entries(root: Path, rel: str) -> ToolOutcome:
    # See _audit_searchable_files: resolve so iterdir entries and the
    # relative_to guards below share one path form.
    root = root.expanduser().resolve()
    reason = _audit_path_block_reason(rel)
    if reason:
        return ToolOutcome.error(reason)
    reason = _audit_raw_symlink_reason(root, rel)
    if reason:
        return ToolOutcome.error(reason)
    try:
        path = safe_join(root, rel)
    except ValueError as exc:
        return ToolOutcome.error(str(exc))
    if not path.is_dir():
        return ToolOutcome.error(f"not a directory: {rel}")
    lines: list[str] = []
    try:
        entries = tuple(sorted(path.iterdir()))
    except OSError as exc:
        return ToolOutcome.error(str(exc))
    for entry in entries:
        try:
            child_rel = entry.relative_to(root).as_posix()
        except ValueError:
            continue
        if _audit_path_block_reason(child_rel):
            continue
        try:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if entry.name in AUDIT_EXCLUDED_DIRS:
                    continue
                lines.append(f"{entry.name}/")
                for sub in sorted(entry.iterdir())[:50]:
                    try:
                        sub_rel = sub.relative_to(root).as_posix()
                    except ValueError:
                        continue
                    if _audit_path_block_reason(sub_rel) or sub.is_symlink():
                        continue
                    tag = "/" if sub.is_dir() else ""
                    lines.append(f"  {sub.name}{tag}")
            elif entry.is_file() and _audit_file_allowed(entry, root)[0]:
                lines.append(entry.name)
        except OSError:
            continue
    return ToolOutcome("\n".join(lines) if lines else "(empty)", True)


def _audit_read_file(root: Path, rel: str, **options: Any) -> ToolOutcome:
    # See _audit_searchable_files: resolve so safe_join output and the
    # _audit_file_allowed guard share one path form.
    root = root.expanduser().resolve()
    reason = _audit_path_block_reason(rel)
    if reason:
        return ToolOutcome.error(reason)
    reason = _audit_raw_symlink_reason(root, rel)
    if reason:
        return ToolOutcome.error(reason)
    try:
        path = safe_join(root, rel)
    except ValueError as exc:
        return ToolOutcome.error(str(exc))
    allowed, reason = _audit_file_allowed(path, root)
    if not allowed:
        return ToolOutcome.error(reason)
    return read_file(root, rel, **options)


def _audit_dir_allowed(path: Path, root: Path) -> bool:
    try:
        rel = path.relative_to(root).as_posix()
    except ValueError:
        return False
    return not _audit_path_block_reason(rel)


def _audit_scan_budget() -> BoundedScanBudget:
    return BoundedScanBudget(
        max_files=PROJECT_AUDIT_MAX_SCAN_FILES,
        max_dirs=PROJECT_AUDIT_MAX_SCAN_DIRS,
        max_dir_entries=PROJECT_AUDIT_MAX_DIR_ENTRIES,
    )


def _audit_searchable_files(root: Path, start: Path, budget: BoundedScanBudget) -> Any:
    # Resolve once: `start` comes from safe_join (resolved) while callers
    # may pass a symlinked/short-name root (e.g. CI temp dirs). Comparing
    # resolved entries against an unresolved root makes every allow_* guard
    # fail closed with ValueError, silently emptying the whole scan.
    resolved_root = root.expanduser().resolve()
    return iter_bounded_files(
        start,
        excluded_dirs=AUDIT_EXCLUDED_DIRS,
        budget=budget,
        allow_dir=lambda path: _audit_dir_allowed(path, resolved_root),
        allow_file=lambda path: _audit_scannable_file_allowed(path, resolved_root),
        skip_start_if_excluded=start.resolve() != resolved_root,
    )


def _audit_search_resolve_start(
    root: Path,
    rel: str,
) -> tuple[Path | None, ToolOutcome | None]:
    reason = _audit_path_block_reason(rel)
    if reason:
        return None, ToolOutcome.error(reason)
    reason = _audit_raw_symlink_reason(root, rel)
    if reason:
        return None, ToolOutcome.error(reason)
    try:
        start = safe_join(root, rel or ".")
    except ValueError as exc:
        return None, ToolOutcome.error(str(exc))
    if not start.exists():
        return None, ToolOutcome.error(f"path not found: {rel}")
    return start, None


def _audit_search_scan_one_file(
    path: Path,
    bytes_read: int,
) -> tuple[str | None, int, bool, bool]:
    try:
        size = path.stat().st_size
        if size > SEARCH_MAX_FILE_BYTES:
            return None, bytes_read, True, False
        if bytes_read + size > SEARCH_MAX_SCAN_BYTES:
            return None, bytes_read, False, True
        bytes_read += size
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None, bytes_read, False, False
    return text, bytes_read, False, False


def _audit_search_collect_file_matches(
    path: Path,
    root: Path,
    text: str,
    needle: str,
    matches: list[str],
    max_results: int,
) -> bool:
    for line_no, line in enumerate(text.splitlines(), start=1):
        if needle not in line.lower():
            continue
        rel_path = path.relative_to(root).as_posix()
        clean = line.strip()
        if len(clean) > 240:
            clean = clean[:237] + "..."
        matches.append(f"{rel_path}:{line_no}: {clean}")
        if len(matches) >= max_results:
            return True
    return False


def _audit_search_append_limit_notes(
    matches: list[str],
    *,
    max_results: int,
    result_limited: bool,
    oversized_files: int,
    byte_limited: bool,
    budget: BoundedScanBudget,
) -> None:
    if not matches:
        matches.append("(no literal matches; regex is not supported)")
    if result_limited:
        matches.append(f"... truncated after {max_results} matches")
    if oversized_files:
        matches.append(
            f"... skipped {oversized_files} oversized file(s); omitted files may "
            "contain more matches"
        )
    if byte_limited:
        matches.append(
            "... project audit search reached its read budget; omitted files may "
            "contain more matches"
        )
    if budget.limited:
        matches.append(budget.stop_message("project audit search scan"))


def _audit_search_build_outcome(
    matches: list[str],
    *,
    result_limited: bool,
    oversized_files: int,
    byte_limited: bool,
    budget: BoundedScanBudget,
) -> ToolOutcome:
    output = "\n".join(matches)
    truncated = result_limited or budget.limited or byte_limited or bool(oversized_files)
    if len(output) > READ_MAX_CHARS:
        output = output[:READ_MAX_CHARS].rstrip() + "\n... truncated"
        truncated = True
    return ToolOutcome(output, True, truncated=truncated)


def _audit_search_files(
    root: Path,
    rel: str,
    query: str,
    *,
    max_results: int = SEARCH_MAX_RESULTS,
) -> ToolOutcome:
    query = query.strip()
    if not query:
        return ToolOutcome.error("search query required")
    start, error = _audit_search_resolve_start(root, rel)
    if error is not None:
        return error
    if start is None:
        return ToolOutcome.error("path could not be resolved")
    # See _audit_searchable_files: match results must be relativized
    # against the same resolved form the scanner yields.
    root = root.expanduser().resolve()
    needle = query.lower()
    matches: list[str] = []
    result_limited = False
    bytes_read = 0
    byte_limited = False
    oversized_files = 0
    budget = _audit_scan_budget()
    for path in _audit_searchable_files(root, start, budget):
        text, bytes_read, oversized_hit, byte_hit = _audit_search_scan_one_file(
            path, bytes_read
        )
        if byte_hit:
            byte_limited = True
            break
        if oversized_hit:
            oversized_files += 1
            continue
        if text is None:
            continue
        if _audit_search_collect_file_matches(
            path, root, text, needle, matches, max_results
        ):
            result_limited = True
            break
    _audit_search_append_limit_notes(
        matches,
        max_results=max_results,
        result_limited=result_limited,
        oversized_files=oversized_files,
        byte_limited=byte_limited,
        budget=budget,
    )
    return _audit_search_build_outcome(
        matches,
        result_limited=result_limited,
        oversized_files=oversized_files,
        byte_limited=byte_limited,
        budget=budget,
    )


def _audit_find_references(root: Path, rel: str, symbol: str) -> ToolOutcome:
    reason = _audit_path_block_reason(rel)
    if reason:
        return ToolOutcome.error(reason)
    reason = _audit_raw_symlink_reason(root, rel)
    if reason:
        return ToolOutcome.error(reason)
    try:
        start = safe_join(root, rel or ".")
    except ValueError as exc:
        return ToolOutcome.error(str(exc))
    if not start.exists():
        return ToolOutcome.error(f"path not found: {rel}")
    try:
        budget = _audit_scan_budget()
        files = _audit_searchable_files(root, start, budget)
        scan = find_reference_hints(
            root,
            start,
            symbol,
            files=files,
            scan_budget=budget,
            files_budgeted=True,
        )
    except ValueError as exc:
        return ToolOutcome.error(str(exc))
    return ToolOutcome(scan.output, True, truncated=scan.truncated)


def _execute_read_only_call(project: Path, call: ToolCall) -> ToolOutcome:
    path = str(call.args.get("path") or ".")
    if call.name == "ls":
        return _audit_visible_entries(project, path)
    if call.name == "read":
        read_options = {
            name: call.args[name]
            for name in ("offset", "limit")
            if name in call.args
        }
        return _audit_read_file(project, path, **read_options)
    if call.name == "search":
        return _audit_search_files(project, path, str(call.args.get("query") or ""))
    if call.name == "references":
        return _audit_find_references(project, path, str(call.args.get("symbol") or ""))
    return ToolOutcome.error(
        "project audit advisors may only use read-only list_dir, read_file, grep, and find_references"
    )


def visible_entries(root: Path, rel: str) -> ToolOutcome:
    """List directory entries visible to project audit advisors."""
    return _audit_visible_entries(root, rel)


def execute_read_only_call(project: Path, call: ToolCall) -> ToolOutcome:
    """Execute one read-only audit call within audit bounds."""
    return _execute_read_only_call(project, call)


__all__ = [
    "AUDIT_BINARY_SUFFIXES",
    "AUDIT_EXCLUDED_DIRS",
    "AUDIT_SECRET_FILENAMES",
    "AUDIT_SECRET_NAME_PARTS",
    "AUDIT_SECRET_SUFFIXES",
    "PROJECT_AUDIT_MAX_DIR_ENTRIES",
    "PROJECT_AUDIT_MAX_FILE_BYTES",
    "PROJECT_AUDIT_MAX_SCAN_DIRS",
    "PROJECT_AUDIT_MAX_SCAN_FILES",
    "execute_read_only_call",
    "visible_entries",
]
