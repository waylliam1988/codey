"""Small changed-symbol extraction from collected changes.

This is a lexical diff helper, not an AST index, call graph, or rename engine.
It only reports obvious declarations that changed in the visible diff.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Mapping, Sequence

from codey.change_set import ChangeSet


MAX_CHANGED_SYMBOLS = 8
IDENTIFIER_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
HUNK_RE = re.compile(
    r"^@@\s+-(?P<old_start>\d+)(?:,(?P<old_lines>\d+))?\s+"
    r"\+(?P<new_start>\d+)(?:,(?P<new_lines>\d+))?\s+@@"
)
PY_DEF_RE = re.compile(r"^\s*(?:async\s+def|def)\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")
PY_CLASS_RE = re.compile(r"^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)\b")
PY_CONST_RE = re.compile(r"^\s*([A-Z][A-Z0-9_]{2,})\s*(?::[^=]+)?=")
JS_FUNC_RE = re.compile(
    r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*\("
)
JS_CLASS_RE = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][A-Za-z0-9_$]*)\b"
)
JS_CONST_RE = re.compile(
    r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*(?::[^=]+)?="
)


@dataclass(frozen=True)
class ChangedSymbol:
    path: str
    name: str
    kind: str
    hunk_index: int
    new_line: int | None = None
    old_name: str = ""

    @property
    def lookup_name(self) -> str:
        return self.old_name or self.name

    @property
    def label(self) -> str:
        if self.old_name and self.old_name != self.name:
            return f"{self.old_name} -> {self.name}"
        return self.name


@dataclass(frozen=True)
class _DiffDef:
    kind: str
    name: str
    hunk_index: int
    line: int | None


def changed_symbols_from_changes(
    changes: Mapping[str, object] | ChangeSet | object,
    *,
    max_symbols: int = MAX_CHANGED_SYMBOLS,
) -> tuple[ChangedSymbol, ...]:
    if max_symbols <= 0:
        return ()
    change_set = changes if isinstance(changes, ChangeSet) else ChangeSet.from_changes(changes)
    if not change_set.raw_diff.strip():
        return ()
    return _symbols_from_diff(change_set, max_symbols=max_symbols)


def changed_symbol_names(
    values: Sequence[ChangedSymbol] | Mapping[str, object] | ChangeSet | object,
    *,
    include_old_names: bool = True,
    max_symbols: int = MAX_CHANGED_SYMBOLS,
) -> tuple[str, ...]:
    if isinstance(values, ChangeSet) or isinstance(values, Mapping):
        symbols = changed_symbols_from_changes(values, max_symbols=max_symbols)
    elif isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
        symbols = tuple(item for item in values if isinstance(item, ChangedSymbol))
    else:
        symbols = ()
    names: list[str] = []
    for symbol in symbols:
        candidates = []
        if include_old_names and symbol.old_name:
            candidates.append(symbol.old_name)
        candidates.append(symbol.name)
        for name in candidates:
            if name and name not in names:
                names.append(name)
        if len(names) >= max_symbols:
            break
    return tuple(names[:max_symbols])


def _symbols_from_diff(
    change_set: ChangeSet,
    *,
    max_symbols: int,
) -> tuple[ChangedSymbol, ...]:
    default_path = change_set.files[0].path if change_set.files else ""
    current_path = default_path
    hunk_index = 1 if default_path else 0
    old_line: int | None = None
    new_line: int | None = None
    pending_old_path = ""
    old_defs: list[_DiffDef] = []
    new_defs: list[_DiffDef] = []
    symbols: list[ChangedSymbol] = []
    seen: set[tuple[str, str, str]] = set()

    def flush() -> None:
        nonlocal old_defs
        nonlocal new_defs
        if current_path and (old_defs or new_defs):
            _append_symbols(
                symbols,
                seen,
                current_path,
                old_defs,
                new_defs,
                fallback_hunk=max(1, hunk_index),
                max_symbols=max_symbols,
            )
        old_defs = []
        new_defs = []

    for raw_line in change_set.raw_diff.replace("\r\n", "\n").replace("\r", "\n").splitlines():
        if len(symbols) >= max_symbols:
            break
        if raw_line.startswith("diff --git "):
            flush()
            current_path = _normalize_path(change_set, _path_from_diff_git(raw_line))
            hunk_index = 0
            old_line = None
            new_line = None
            pending_old_path = ""
            continue
        if raw_line.startswith("--- "):
            pending_old_path = _diff_path(raw_line[4:])
            continue
        if raw_line.startswith("+++ "):
            flush()
            pending_new_path = _diff_path(raw_line[4:])
            selected = pending_new_path or pending_old_path or current_path
            current_path = _normalize_path(change_set, selected)
            hunk_index = 0
            old_line = None
            new_line = None
            continue
        if raw_line.startswith("@@"):
            flush()
            hunk_index += 1
            match = HUNK_RE.match(raw_line)
            old_line = int(match.group("old_start")) if match else None
            new_line = int(match.group("new_start")) if match else None
            continue
        if raw_line.startswith("\\"):
            continue
        if raw_line.startswith("-") and not raw_line.startswith("---"):
            for kind, name in _defs_from_line(raw_line[1:]):
                old_defs.append(_DiffDef(kind, name, max(1, hunk_index), old_line))
            if old_line is not None:
                old_line += 1
            continue
        if raw_line.startswith("+") and not raw_line.startswith("+++"):
            for kind, name in _defs_from_line(raw_line[1:]):
                new_defs.append(_DiffDef(kind, name, max(1, hunk_index), new_line))
            if new_line is not None:
                new_line += 1
            continue
        if raw_line.startswith(" "):
            if old_line is not None:
                old_line += 1
            if new_line is not None:
                new_line += 1
    flush()
    return tuple(symbols[:max_symbols])


def _append_symbols(
    symbols: list[ChangedSymbol],
    seen: set[tuple[str, str, str]],
    path: str,
    old_defs: list[_DiffDef],
    new_defs: list[_DiffDef],
    *,
    fallback_hunk: int,
    max_symbols: int,
) -> None:
    paired_old: set[int] = set()
    paired_new: set[int] = set()
    for old_index, new_index in _paired_definition_indices(old_defs, new_defs):
        old = old_defs[old_index]
        new = new_defs[new_index]
        paired_old.add(old_index)
        paired_new.add(new_index)
        old_name = old.name if old.name != new.name else ""
        _append_symbol(
            symbols,
            seen,
            ChangedSymbol(
                path=path,
                name=new.name,
                kind=new.kind or old.kind,
                hunk_index=new.hunk_index or old.hunk_index or fallback_hunk,
                new_line=new.line,
                old_name=old_name,
            ),
            max_symbols=max_symbols,
        )
    for index, old in enumerate(old_defs):
        if index in paired_old:
            continue
        _append_symbol(
            symbols,
            seen,
            ChangedSymbol(
                path=path,
                name=old.name,
                old_name=old.name,
                kind=old.kind,
                hunk_index=old.hunk_index or fallback_hunk,
            ),
            max_symbols=max_symbols,
        )
    for index, new in enumerate(new_defs):
        if index in paired_new:
            continue
        _append_symbol(
            symbols,
            seen,
            ChangedSymbol(
                path=path,
                name=new.name,
                kind=new.kind,
                hunk_index=new.hunk_index or fallback_hunk,
                new_line=new.line,
            ),
            max_symbols=max_symbols,
        )


def _paired_definition_indices(
    old_defs: list[_DiffDef],
    new_defs: list[_DiffDef],
) -> tuple[tuple[int, int], ...]:
    pairs: list[tuple[int, int]] = []
    for kind in _kind_order(old_defs, new_defs):
        old_indices = [index for index, item in enumerate(old_defs) if item.kind == kind]
        new_indices = [index for index, item in enumerate(new_defs) if item.kind == kind]
        pairs.extend(zip(old_indices, new_indices))
    return tuple(pairs)


def _kind_order(
    old_defs: list[_DiffDef],
    new_defs: list[_DiffDef],
) -> tuple[str, ...]:
    kinds: list[str] = []
    for item in (*old_defs, *new_defs):
        if item.kind not in kinds:
            kinds.append(item.kind)
    return tuple(kinds)


def _append_symbol(
    symbols: list[ChangedSymbol],
    seen: set[tuple[str, str, str]],
    symbol: ChangedSymbol,
    *,
    max_symbols: int,
) -> None:
    if len(symbols) >= max_symbols or not symbol.path or not symbol.name:
        return
    key = (symbol.path, symbol.name, symbol.old_name)
    if key in seen:
        return
    seen.add(key)
    symbols.append(symbol)


def _defs_from_line(line: str) -> tuple[tuple[str, str], ...]:
    for kind, regex in (
        ("function", PY_DEF_RE),
        ("class", PY_CLASS_RE),
        ("constant", PY_CONST_RE),
        ("function", JS_FUNC_RE),
        ("class", JS_CLASS_RE),
        ("constant", JS_CONST_RE),
    ):
        match = regex.search(line)
        if not match:
            continue
        name = match.group(1)
        if IDENTIFIER_RE.fullmatch(name):
            return ((kind, name),)
        return ()
    return ()


def _normalize_path(change_set: ChangeSet, path: str) -> str:
    file = change_set.file_for_path(path)
    if file is not None:
        return file.path
    return _safe_relpath(path)


def _path_from_diff_git(line: str) -> str:
    parts = line.split()
    if len(parts) >= 4:
        return _diff_path(parts[3])
    return ""


def _diff_path(value: str) -> str:
    token = value.split("\t", 1)[0].strip().strip('"')
    if token == "/dev/null":
        return ""
    if token.startswith("a/") or token.startswith("b/"):
        token = token[2:]
    return _safe_relpath(token)


def _safe_relpath(value: str) -> str:
    normalized = value.replace("\\", "/").strip().strip("/")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts:
        return ""
    return path.as_posix()
