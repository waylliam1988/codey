"""Bounded verification candidates derived from local project evidence.

This is not impact analysis or coverage proof.  It scans likely test files once
and reports only explainable naming, direct-import, and changed-symbol hints.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Sequence

from codey.bounded_scan import BoundedScanBudget, iter_bounded_files
from codey.changed_symbols import changed_symbol_names
from codey.project_map import (
    EXCLUDED_DIRS,
    LOCK_FILENAMES,
    SECRET_FILENAMES,
    SECRET_NAME_PARTS,
    SECRET_SUFFIXES,
)


MAX_CHANGED_FILES = 20
MAX_SCAN_FILES = 400
MAX_SCAN_DIRS = 160
MAX_DIR_ENTRIES = 800
MAX_TEST_FILE_BYTES = 512 * 1024
MAX_SCAN_TOTAL_BYTES = 16 * 1024 * 1024
MAX_CANDIDATES = 12
MAX_OBSERVED_CHECKS = 8
MAX_BROADER_COMMANDS = 6
MAX_RENDER_CHARS = 5_000

JS_IMPORT_RE = re.compile(
    r"(?:from\s*|require\s*\(|import\s*\()\s*['\"]([^'\"]+)['\"]"
)


@dataclass(frozen=True)
class TestCandidate:
    path: str
    reason: str
    evidence: str


@dataclass(frozen=True)
class ObservedCheck:
    command: str
    cwd: str = "."


@dataclass(frozen=True)
class VerificationMap:
    changed_files: tuple[str, ...] = ()
    changed_tests: tuple[str, ...] = ()
    test_candidates: tuple[TestCandidate, ...] = ()
    observed_checks: tuple[ObservedCheck, ...] = ()
    recommended_commands: tuple[str, ...] = ()
    broader_commands: tuple[str, ...] = ()
    truncated: bool = False

    def render(self) -> str:
        lines = ["Verification Map (bounded candidates; not coverage proof):"]
        _section(lines, "Changed files", self.changed_files)
        _section(lines, "Changed tests", self.changed_tests, fallback="(none)")
        lines.append("Existing test candidates found locally (not necessarily changed):")
        if self.test_candidates:
            for item in self.test_candidates:
                lines.append(f"- {item.path}: {item.reason} [evidence: {item.evidence}]")
        else:
            lines.append("- (none found; this does not prove that no relevant tests exist)")
        lines.append("Observed successful checks after the latest edit:")
        if self.observed_checks:
            for item in self.observed_checks:
                suffix = f" (cwd {item.cwd})" if item.cwd != "." else ""
                lines.append(f"- {item.command}{suffix}")
        else:
            lines.append("- (none observed)")
        if self.recommended_commands:
            _section(lines, "Recommended local check candidates", self.recommended_commands)
        else:
            _section(
                lines,
                "Broader check candidates (inspect relevance before requesting)",
                self.broader_commands,
                fallback="(none found)",
            )
        if self.truncated:
            lines.append(
                "- Candidate discovery was truncated; additional relevant tests may exist."
            )
        text = "\n".join(lines)
        if len(text) <= MAX_RENDER_CHARS:
            return text
        return text[:MAX_RENDER_CHARS].rstrip() + "\n- map truncated by character budget"


def build_verification_map(
    project: str | Path,
    changes: dict,
    *,
    checks_after_last_change: Sequence[object] = (),
    project_map: str = "",
    recommended_commands: Sequence[str] = (),
) -> VerificationMap:
    root = Path(project).expanduser().resolve()
    changed = _changed_paths(changes)
    changed_tests = tuple(path for path in changed if _is_test_path(path))
    changed_sources = tuple(path for path in changed if path not in changed_tests)
    symbols = changed_symbol_names(changes, include_old_names=True)
    candidates: dict[str, TestCandidate] = {}
    bytes_read = 0
    byte_limited = False
    budget = BoundedScanBudget(
        max_files=MAX_SCAN_FILES,
        max_dirs=MAX_SCAN_DIRS,
        max_dir_entries=MAX_DIR_ENTRIES,
    )
    if changed_sources:
        for path in iter_bounded_files(
            root,
            excluded_dirs=EXCLUDED_DIRS,
            budget=budget,
            allow_dir=lambda item: _allowed_test_dir(root, item),
            allow_file=lambda item: _allowed_test_file(root, item),
            skip_start_if_excluded=False,
        ):
            rel = path.relative_to(root).as_posix()
            if rel in changed_tests:
                continue
            try:
                size = path.stat().st_size
                if size > MAX_TEST_FILE_BYTES:
                    continue
                if bytes_read + size > MAX_SCAN_TOTAL_BYTES:
                    byte_limited = True
                    break
                bytes_read += size
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            candidate = _candidate_for_test(
                root,
                path,
                rel,
                text,
                changed_sources,
                symbols,
            )
            if candidate is not None:
                candidates[rel] = candidate
            if len(candidates) > MAX_CANDIDATES:
                break
    truncated = (
        bool(changes.get("truncated"))
        or budget.limited
        or byte_limited
        or len(candidates) > MAX_CANDIDATES
    )
    safe_recommended = _recommended_commands(recommended_commands)
    return VerificationMap(
        changed_files=changed,
        changed_tests=changed_tests,
        test_candidates=tuple(candidates.values())[:MAX_CANDIDATES],
        observed_checks=_observed_checks(checks_after_last_change),
        recommended_commands=safe_recommended,
        broader_commands=() if safe_recommended else _broader_commands(project_map),
        truncated=truncated,
    )


def render_verification_map(*args, **kwargs) -> str:
    return build_verification_map(*args, **kwargs).render()


def _section(lines: list[str], title: str, values: Sequence[str], fallback: str = "") -> None:
    lines.append(f"{title}:")
    if values:
        lines.extend(f"- {item}" for item in values)
    elif fallback:
        lines.append(f"- {fallback}")


def _changed_paths(changes: dict) -> tuple[str, ...]:
    result: list[str] = []
    for item in changes.get("files") or []:
        if len(result) >= MAX_CHANGED_FILES or not isinstance(item, dict):
            break
        value = str(item.get("path") or "").replace("\\", "/").strip("/")
        path = PurePosixPath(value)
        if not value or path.is_absolute() or ".." in path.parts:
            continue
        if value not in result:
            result.append(value)
    return tuple(result)


def _is_test_path(rel: str) -> bool:
    path = PurePosixPath(rel)
    lower_parts = [part.lower() for part in path.parts]
    name = path.name.lower()
    return (
        any(part in {"test", "tests", "__tests__"} for part in lower_parts[:-1])
        or name.startswith("test_")
        or name.endswith("_test.py")
        or ".test." in name
        or ".spec." in name
    )


def _allowed_test_file(root: Path, path: Path) -> bool:
    try:
        rel = path.relative_to(root).as_posix()
    except ValueError:
        return False
    if not _is_test_path(rel):
        return False
    for part in PurePosixPath(rel).parts:
        lower = part.lower()
        if lower.startswith(".") or lower in SECRET_FILENAMES or lower in LOCK_FILENAMES:
            return False
        if any(lower.endswith(suffix) for suffix in SECRET_SUFFIXES):
            return False
    return True


def _allowed_test_dir(root: Path, path: Path) -> bool:
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return False
    for part in parts:
        lower = part.lower()
        if lower.startswith(".") or lower in SECRET_FILENAMES:
            return False
        if any(marker in lower for marker in SECRET_NAME_PARTS):
            return False
        if any(lower.endswith(suffix) for suffix in SECRET_SUFFIXES):
            return False
    return True


def _candidate_for_test(
    root: Path,
    path: Path,
    rel: str,
    text: str,
    changed_sources: Sequence[str],
    symbols: Sequence[str],
) -> TestCandidate | None:
    for source in changed_sources:
        if _naming_match(rel, source):
            return TestCandidate(rel, f"name corresponds to changed file {source}", "naming")
    suffix = path.suffix.lower()
    if suffix == ".py":
        imports = _python_imports(text)
        for source in changed_sources:
            if _python_import_matches(source, imports):
                return TestCandidate(rel, f"directly imports changed module {source}", "import")
    elif suffix in {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}:
        for source in changed_sources:
            if _javascript_import_matches(root, path, source, text):
                return TestCandidate(rel, f"directly imports changed module {source}", "import")
    for symbol in symbols:
        if re.search(rf"(?<![\w$]){re.escape(symbol)}(?![\w$])", text):
            return TestCandidate(rel, f"references changed declaration {symbol}", "reference")
    return None


def _naming_match(test: str, source: str) -> bool:
    test_path = PurePosixPath(test)
    source_path = PurePosixPath(source)
    stem = source_path.stem.lower()
    name = test_path.name.lower()
    if source_path.suffix.lower() == ".py":
        return name in {f"test_{stem}.py", f"{stem}_test.py"}
    return name in {
        f"{stem}.test.js", f"{stem}.test.jsx", f"{stem}.test.ts", f"{stem}.test.tsx",
        f"{stem}.spec.js", f"{stem}.spec.jsx", f"{stem}.spec.ts", f"{stem}.spec.tsx",
    } or ("__tests__" in (part.lower() for part in test_path.parts) and test_path.stem.lower() == stem)


def _python_imports(text: str) -> set[str]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return set()
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
            imports.update(f"{node.module}.{alias.name}" for alias in node.names)
    return imports


def _python_import_matches(source: str, imports: set[str]) -> bool:
    path = PurePosixPath(source)
    if path.suffix.lower() != ".py":
        return False
    parts = list(path.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    variants = {".".join(parts)} if parts else set()
    if len(parts) > 1 and parts[0].lower() in {"src", "lib"}:
        variants.add(".".join(parts[1:]))
    return bool(variants & imports)


def _javascript_import_matches(root: Path, test: Path, source: str, text: str) -> bool:
    source_path = (root / source).resolve()
    source_no_ext = source_path.with_suffix("")
    for specifier in JS_IMPORT_RE.findall(text):
        if not specifier.startswith("."):
            continue
        resolved = (test.parent / specifier).resolve()
        if resolved == source_path or resolved.with_suffix("") == source_no_ext:
            return True
        if resolved.is_dir() and resolved / "index" == source_no_ext:
            return True
    return False


def _observed_checks(values: Sequence[object]) -> tuple[ObservedCheck, ...]:
    result: list[ObservedCheck] = []
    for value in values:
        command = str(getattr(value, "command", "") or "").strip()
        cwd = str(getattr(value, "cwd", ".") or ".").strip() or "."
        if isinstance(value, dict):
            command = str(value.get("command") or "").strip()
            cwd = str(value.get("cwd") or ".").strip() or "."
        elif isinstance(value, str):
            command = value.strip()
        if command:
            item = ObservedCheck(command[:500], cwd[:240])
            if item not in result:
                result.append(item)
        if len(result) >= MAX_OBSERVED_CHECKS:
            break
    return tuple(result)


def _recommended_commands(values: Sequence[str]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        command = str(value or "").replace("\r", " ").replace("\n", " ").strip()
        if command and command not in result:
            result.append(command[:500])
        if len(result) >= MAX_BROADER_COMMANDS:
            break
    return tuple(result)


def _broader_commands(project_map: str) -> tuple[str, ...]:
    result: list[str] = []
    section = ""
    for line in (project_map or "").splitlines():
        stripped = line.strip()
        if stripped == "Observed successful checks:":
            section = "observed"
            continue
        if stripped == "Candidate commands (inspect before running):":
            section = "candidate"
            continue
        if section and stripped.endswith(":"):
            section = ""
            continue
        if section and stripped.startswith("- "):
            command = stripped[2:].strip()
            if section == "observed":
                match = re.fullmatch(
                    r"successful check(?: from ([^:]+))?: (.+)",
                    command,
                )
                if not match:
                    continue
                cwd, command = match.groups()
                if cwd and cwd != ".":
                    command = f"{cwd.rstrip('/')}/: {command}"
            if command and command not in result:
                result.append(command)
        if len(result) >= MAX_BROADER_COMMANDS:
            break
    return tuple(result)
