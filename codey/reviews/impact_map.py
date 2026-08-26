"""Bounded review-only impact hints for changed symbols.

This is not a dependency graph, LSP index, or coverage proof. It gives the
reviewer a short list of local lexical reference hints so review can inspect
possible callers and tests without expanding Codey's product surface.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable

from codey.runtime import cancellation
from codey.workspace.bounded_scan import BoundedScanBudget, iter_bounded_files
from codey.workspace.change_set import ChangeSet
from codey.workspace.changed_symbols import ChangedSymbol, changed_symbols_from_changes
from codey.utils.references import REFERENCE_EXCLUDED_DIRS, find_reference_hints


MAX_CHANGED_SYMBOLS = 3
MAX_REFS_PER_SYMBOL = 4
MAX_RENDERED_REFS = 10
MAX_RENDER_CHARS = 1_800
MAX_SCAN_FILES = 800
MAX_SCAN_DIRS = 200
MAX_DIR_ENTRIES = 800
MAX_SCAN_BYTES = 2 * 1024 * 1024
SOURCE_SUFFIXES = {
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".mjs",
    ".cjs",
    ".mts",
    ".cts",
}
REF_ROW_RE = re.compile(r"^- (?P<kind>[A-Za-z_]+) (?P<path>.+?):(?P<line>\d+):")


@dataclass(frozen=True)
class ImpactReference:
    symbol: str
    path: str
    line: int
    kind: str


def safe_review_impact_map(project: str | Path, changes: object) -> str:
    try:
        return render_review_impact_map(project, changes)
    except cancellation.TaskCancelled:
        raise
    except Exception:
        return ""


def render_review_impact_map(
    project: str | Path,
    changes: object,
    *,
    max_symbols: int = MAX_CHANGED_SYMBOLS,
    max_refs_per_symbol: int = MAX_REFS_PER_SYMBOL,
    max_rendered_refs: int = MAX_RENDERED_REFS,
    max_chars: int = MAX_RENDER_CHARS,
) -> str:
    root = Path(project).expanduser().resolve()
    if not root.is_dir():
        return ""
    change_set = changes if isinstance(changes, ChangeSet) else ChangeSet.from_changes(changes)
    symbols = changed_symbols_from_changes(change_set, max_symbols=max_symbols)
    if not symbols:
        return ""
    changed_paths = _changed_path_set(change_set)
    files, limited = _candidate_source_files(root, changed_paths)
    refs: list[ImpactReference] = []
    incomplete = limited
    for symbol in symbols:
        symbol_refs, symbol_incomplete = _references_for_symbol(
            root,
            files,
            symbol,
            changed_paths,
            max_refs=max_refs_per_symbol,
        )
        refs.extend(symbol_refs)
        incomplete = incomplete or symbol_incomplete
    refs, capped = _cap_references_for_render(refs, max_rendered_refs)
    incomplete = incomplete or capped
    return _render(symbols, refs, incomplete=incomplete, max_chars=max_chars)


def _candidate_source_files(
    root: Path,
    excluded_rels: set[str],
) -> tuple[tuple[Path, ...], bool]:
    budget = BoundedScanBudget(
        max_files=MAX_SCAN_FILES,
        max_dirs=MAX_SCAN_DIRS,
        max_dir_entries=MAX_DIR_ENTRIES,
        max_bytes=MAX_SCAN_BYTES,
    )

    def allow_file(path: Path) -> bool:
        return (
            path.suffix.lower() in SOURCE_SUFFIXES
            and _relative(root, path) not in excluded_rels
        )

    files = tuple(
        iter_bounded_files(
            root,
            excluded_dirs=REFERENCE_EXCLUDED_DIRS,
            budget=budget,
            allow_file=allow_file,
            skip_start_if_excluded=False,
        )
    )
    return files, budget.limited


def _references_for_symbol(
    root: Path,
    files: tuple[Path, ...],
    symbol: ChangedSymbol,
    changed_paths: set[str],
    *,
    max_refs: int,
) -> tuple[tuple[ImpactReference, ...], bool]:
    if max_refs <= 0:
        return (), False
    caller_files, test_files = _split_reference_files(root, files)
    test_quota = min(1, max_refs)
    test_refs, test_incomplete = _scan_reference_files(
        root,
        test_files,
        symbol,
        changed_paths,
        max_refs=test_quota,
    )
    caller_quota = max_refs - len(test_refs)
    caller_refs, caller_incomplete = _scan_reference_files(
        root,
        caller_files,
        symbol,
        changed_paths,
        max_refs=caller_quota,
    )
    return (
        (*caller_refs, *test_refs),
        caller_incomplete or test_incomplete,
    )


def _split_reference_files(
    root: Path,
    files: tuple[Path, ...],
) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    caller_files: list[Path] = []
    test_files: list[Path] = []
    for path in files:
        rel = _relative(root, path)
        if _is_test_path(rel):
            test_files.append(path)
        else:
            caller_files.append(path)
    return tuple(caller_files), tuple(test_files)


def _scan_reference_files(
    root: Path,
    files: tuple[Path, ...],
    symbol: ChangedSymbol,
    changed_paths: set[str],
    *,
    max_refs: int,
) -> tuple[tuple[ImpactReference, ...], bool]:
    if max_refs <= 0 or not files:
        return (), False
    try:
        scan = find_reference_hints(
            root,
            root,
            symbol.lookup_name,
            files=files,
            files_budgeted=True,
            max_results=max_refs,
        )
    except ValueError:
        return (), False

    refs: list[ImpactReference] = []
    seen: set[tuple[str, int, str, str]] = set()
    for line in scan.output.splitlines():
        match = REF_ROW_RE.match(line)
        if not match:
            continue
        path = match.group("path")
        if path in changed_paths:
            continue
        line_no = int(match.group("line"))
        kind = match.group("kind")
        key = (symbol.lookup_name, path, line_no, kind)
        if key in seen:
            continue
        seen.add(key)
        refs.append(
            ImpactReference(
                symbol=symbol.lookup_name,
                path=path,
                line=line_no,
                kind=kind,
            )
        )
        if len(refs) >= max_refs:
            break
    incomplete = scan.truncated or bool(scan.report and scan.report.incomplete)
    return tuple(refs), incomplete


def _cap_references_for_render(
    refs: list[ImpactReference],
    max_rendered_refs: int,
) -> tuple[list[ImpactReference], bool]:
    if max_rendered_refs <= 0:
        return [], bool(refs)
    if len(refs) <= max_rendered_refs:
        return refs, False
    kept: list[ImpactReference] = []
    seen: set[tuple[str, str, int, str]] = set()
    symbols = _ordered_symbols(refs)

    def take(predicate: Callable[[ImpactReference], bool], *, limit: int | None = None) -> None:
        if len(kept) >= max_rendered_refs:
            return
        taken = 0
        for ref in refs:
            if not predicate(ref):
                continue
            key = (ref.symbol, ref.path, ref.line, ref.kind)
            if key in seen:
                continue
            kept.append(ref)
            seen.add(key)
            taken += 1
            if len(kept) >= max_rendered_refs:
                return
            if limit is not None and taken >= limit:
                return

    for symbol in symbols:
        take(
            lambda ref, candidate=symbol: ref.symbol == candidate and _is_test_path(ref.path),
            limit=1,
        )
    for symbol in symbols:
        take(
            lambda ref, candidate=symbol: ref.symbol == candidate and not _is_test_path(ref.path),
            limit=1,
        )
    take(lambda _ref: True)
    return kept, True


def _ordered_symbols(refs: list[ImpactReference]) -> tuple[str, ...]:
    symbols: list[str] = []
    for ref in refs:
        if ref.symbol not in symbols:
            symbols.append(ref.symbol)
    return tuple(symbols)


def _render(
    symbols: tuple[ChangedSymbol, ...],
    refs: list[ImpactReference],
    *,
    incomplete: bool,
    max_chars: int,
) -> str:
    lines = ["Review Impact Map (bounded hints; not coverage proof):"]
    lines.append("Changed symbols:")
    for symbol in symbols:
        line = f"- {symbol.path} hunk {symbol.hunk_index}: {symbol.kind} {symbol.label}"
        if symbol.new_line is not None:
            line += f" (new line {symbol.new_line})"
        lines.append(line)

    external_refs = tuple(ref for ref in refs if not _is_test_path(ref.path))
    test_refs = tuple(ref for ref in refs if _is_test_path(ref.path))
    lines.append("External reference hints outside changed files:")
    if external_refs:
        lines.extend(_reference_line(ref) for ref in external_refs)
    else:
        lines.append("- (none found; bounded scan only)")
    lines.append("Test reference hints:")
    if test_refs:
        lines.extend(_reference_line(ref) for ref in test_refs)
    else:
        lines.append("- (none found; bounded scan only)")

    risk_hints = _risk_hints(symbols, external_refs, test_refs)
    if risk_hints:
        lines.append("Risk hints:")
        lines.extend(f"- {hint}" for hint in risk_hints)
    if incomplete:
        lines.append("- scan was bounded; omitted files may contain more references")
    lines.append(
        "Use this only to inspect impact; findings[].path must still be a changed file."
    )
    rendered = "\n".join(lines)
    if len(rendered) <= max_chars:
        return rendered
    return rendered[:max_chars].rstrip() + "\n- impact map truncated"


def _reference_line(ref: ImpactReference) -> str:
    return f"- {ref.symbol}: {ref.path}:{ref.line} ({ref.kind})"


def _risk_hints(
    symbols: tuple[ChangedSymbol, ...],
    external_refs: tuple[ImpactReference, ...],
    test_refs: tuple[ImpactReference, ...],
) -> tuple[str, ...]:
    hints: list[str] = []
    if any(symbol.old_name and symbol.old_name != symbol.name for symbol in symbols):
        hints.append("renamed symbol; inspect imports and callers outside changed files")
    if external_refs:
        hints.append("external reference hints found outside changed files")
    if external_refs and not test_refs:
        hints.append("no direct test reference found in bounded scan")
    return tuple(hints[:4])


def _changed_path_set(change_set: ChangeSet) -> set[str]:
    paths: set[str] = set()
    for file in change_set.files:
        if file.path:
            paths.add(file.path)
        if file.previous_path:
            paths.add(file.previous_path)
    return paths


def _relative(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return ""


def _is_test_path(rel: str) -> bool:
    path = PurePosixPath(rel.replace("\\", "/"))
    lower_parts = [part.lower() for part in path.parts]
    name = path.name.lower()
    return (
        any(part in {"test", "tests", "__tests__"} for part in lower_parts[:-1])
        or name.startswith("test_")
        or name.endswith("_test.py")
        or ".test." in name
        or ".spec." in name
    )