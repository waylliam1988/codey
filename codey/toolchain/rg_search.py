"""The single packaged-ripgrep search engine used by every grep caller."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from typing import Any

from codey.runtime.core import cancellation
from codey.toolchain.result_schema import search_output_schema, validate_output
from codey.toolchain.runtime import ToolOutcome
from codey.toolchain.search_page import page_footer
from codey.workspace.bounded_scan import BoundedScanBudget

SEARCH_TIMEOUT_SECONDS = 30
SEARCH_RAW_BYTES = 1_000_000
ARGV_BYTES = 24_000


class SearchError(ValueError):
    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


def rg_binary() -> str:
    """Use only the executable owned by the required Python distribution."""
    package = distribution("ripgrep-bin")
    names = [p for p in package.files or () if p.name == ("rg.exe" if os.name == "nt" else "rg")]
    if len(names) != 1:
        raise FileNotFoundError("ripgrep-bin must contain exactly one rg executable")
    executable = Path(str(package.locate_file(names[0]))).resolve(strict=True)
    if not executable.is_file():
        raise FileNotFoundError("ripgrep-bin executable is missing")
    return str(executable)


@dataclass
class Scan:
    files: list[Path] = field(default_factory=list)
    bytes: int = 0
    oversized: int = 0
    unreadable: int = 0
    byte_limited: bool = False


@dataclass(frozen=True)
class ParsedEvents:
    matches: list[dict[str, object]]
    contexts: list[dict[str, object]]


def _candidates(files: Iterable[Path], max_file_bytes: int, max_scan_bytes: int) -> Scan:
    scan = Scan()
    for path in files:
        cancellation.check()
        try:
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                continue
            if not stat.S_ISREG(info.st_mode):
                scan.unreadable += 1
                continue
            if info.st_size > max_file_bytes:
                scan.oversized += 1
                continue
            if scan.bytes + info.st_size > max_scan_bytes:
                scan.byte_limited = True
                break
            scan.files.append(path.resolve())
            scan.bytes += info.st_size
        except (OSError, ValueError):
            scan.unreadable += 1
    scan.files.sort(key=lambda p: p.as_posix())
    return scan


def _batches(files: list[Path], base: list[str]) -> Iterable[list[str]]:
    batch: list[str] = []
    size = len(subprocess.list2cmdline(base).encode("utf-16-le"))
    for path in files:
        item = str(path)
        extra = len(subprocess.list2cmdline([item]).encode("utf-16-le")) + 2
        if batch and size + extra > ARGV_BYTES:
            yield batch
            batch = []
            size = len(subprocess.list2cmdline(base).encode("utf-16-le"))
        if size + extra > ARGV_BYTES:
            raise SearchError("search path or pattern exceeds the argument budget", "search_argument_limit")
        batch.append(item)
        size += extra
    if batch:
        yield batch


def _line_text(data: Any) -> tuple[str, str, int]:
    try:
        path_text = data["path"]["text"]
        lines = data["lines"]
        text = lines["text"]
        line = data["line_number"]
        if "bytes" in lines:
            raise SearchError("matching line is not UTF-8 text; search is incomplete", "search_invalid_encoding")
        if not isinstance(path_text, str) or not isinstance(text, str) or type(line) is not int or line < 1:
            raise ValueError("invalid line event")
        if text.endswith("\n"):
            text = text[:-1].removesuffix("\r")
        return path_text, text, line
    except SearchError:
        raise
    except (KeyError, TypeError, IndexError) as exc:
        if isinstance(data, dict) and isinstance(data.get("lines"), dict) and "bytes" in data["lines"]:
            raise SearchError("matching line is not UTF-8 text; search is incomplete", "search_invalid_encoding") from exc
        raise SearchError("malformed ripgrep line event", "search_malformed_output") from exc


def _canonical_path(path_text: str, root: Path, allowed: set[Path] | None) -> str:
    path = Path(path_text)
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    if allowed is not None and path not in allowed:
        raise SearchError("result path was not admitted", "search_malformed_output")
    try:
        info = path.lstat()
    except OSError as exc:
        raise SearchError("result path is no longer readable", "search_malformed_output") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise SearchError("result path is not a regular file", "search_malformed_output")
    try:
        return path.relative_to(root).as_posix()
    except ValueError as exc:
        raise SearchError("result path escaped the workspace", "search_malformed_output") from exc


def _match(data: Any, root: Path, allowed: set[Path] | None) -> dict[str, object]:
    path_text, text, line = _line_text(data)
    parts = data.get("submatches") if isinstance(data, dict) else None
    if not isinstance(parts, list) or not parts:
        raise SearchError("malformed ripgrep match", "search_malformed_output")
    start = parts[0].get("start") if isinstance(parts[0], dict) else None
    if type(start) is not int or start < 0 or start > len(text.encode("utf-8")):
        raise SearchError("malformed ripgrep match", "search_malformed_output")
    return {"path": _canonical_path(path_text, root, allowed), "line": line,
            "column_byte": start + 1, "text": text}


def _context(data: Any, root: Path, allowed: set[Path] | None) -> dict[str, object]:
    path_text, text, line = _line_text(data)
    return {"path": _canonical_path(path_text, root, allowed), "line": line,
            "text": text, "relation": "before"}


def _line_number(value: object) -> int:
    if type(value) is not int:
        raise SearchError("malformed context line", "search_malformed_output")
    return value


def _event_row(raw: str) -> tuple[str, dict[str, object]]:
    row = json.loads(raw)
    kind = row.get("type") if isinstance(row, dict) else None
    if not isinstance(kind, str):
        raise ValueError("unknown ripgrep event")
    data = row.get("data") if isinstance(row, dict) else None
    if not isinstance(data, dict):
        raise ValueError("missing ripgrep event data")
    return kind, data


def _summary_stats(data: dict[str, object], matches: list[dict[str, object]]) -> tuple[int | None, int | None]:
    stats = data.get("stats")
    if not isinstance(stats, dict) or type(stats.get("matched_lines")) is not int:
        raise ValueError("missing ripgrep summary stats")
    if stats["matched_lines"] != len(matches):
        raise ValueError("ripgrep summary differs from observed matches")
    searches = stats.get("searches")
    bytes_searched = stats.get("bytes_searched")
    if searches is not None and (type(searches) is not int or searches < 0):
        raise ValueError("malformed ripgrep search count")
    if bytes_searched is not None and (type(bytes_searched) is not int or bytes_searched < 0):
        raise ValueError("malformed ripgrep byte count")
    return searches, bytes_searched


def _parse_events(output: str, root: Path, allowed: set[Path] | None) -> ParsedEvents:
    matches: list[dict[str, object]] = []
    contexts: list[dict[str, object]] = []
    summary = False
    try:
        raw_lines = output.split("\n")
        if output and not output.endswith("\n"):
            raise ValueError("unterminated ripgrep output frame")
        for raw in raw_lines:
            if not raw:
                continue
            kind, data = _event_row(raw)
            if summary or kind not in {"begin", "end", "match", "context", "summary"}:
                raise ValueError("unknown ripgrep event")
            if kind == "begin":
                path_value = data.get("path")
                path_text = path_value.get("text") if isinstance(path_value, dict) else None
                if not isinstance(path_text, str):
                    raise ValueError("malformed begin path")
                path = Path(path_text)
                if not path.is_absolute():
                    path = root / path
                _canonical_path(str(path), root, allowed)
            elif kind == "match":
                matches.append(_match(data, root, allowed))
            elif kind == "context":
                contexts.append(_context(data, root, allowed))
            elif kind == "summary":
                _summary_stats(data, matches)
                summary = True
        if not summary:
            raise ValueError("missing ripgrep summary")
    except SearchError:
        raise
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise SearchError(f"malformed ripgrep output: {exc}", "search_malformed_output") from exc
    return ParsedEvents(matches, contexts)


@dataclass(frozen=True)
class RgExecutor:
    """Own all rg process, output, cancellation, and JSON semantics."""

    executable: str

    def _run(self, args: list[str], root: Path, timeout: float) -> cancellation.CapturedProcess:
        result = cancellation.run_process(args, cwd=root, timeout=max(0.0, timeout),
                                          capture_limit_bytes=SEARCH_RAW_BYTES)
        if result.stdout_truncated or result.stderr_truncated:
            raise SearchError("ripgrep raw output exceeded its budget; narrow the search", "search_output_limit")
        return result

    def select_files(self, root: Path, files: list[Path], include: str, exclude: tuple[str, ...], deadline: float) -> list[Path]:
        if not include and not exclude:
            return files
        base = [self.executable, "--no-config", "--files", "--null", "--hidden", "--no-ignore", "--no-follow"]
        if include:
            base.append(f"--glob={include}")
        base.extend(f"--glob=!{pattern}" for pattern in exclude)
        allowed = set(files)
        selected: set[Path] = set()
        # Native glob matching only revisits the immediate parents of admitted
        # files. Never recurse into unrelated or excluded subtrees.
        parents = sorted({p.parent for p in files}, key=lambda p: p.as_posix())
        base.append("--max-depth=1")
        for batch in _batches(parents, base):
            cancellation.check()
            result = self._run([*base, "--", *batch], root, deadline - time.monotonic())
            if result.returncode not in (0, 1):
                invalid = "error parsing glob" in result.stderr
                raise SearchError(result.stderr[:1600] or f"ripgrep exited {result.returncode}",
                                  "search_invalid_pattern" if invalid else "search_failed")
            for raw_path in result.stdout.split("\x00"):
                if raw_path:
                    path = Path(raw_path)
                    path = path if path.is_absolute() else root / path
                    if path in allowed:
                        selected.add(path)
        return sorted(selected, key=lambda p: p.as_posix())

    def search_batch(self, root: Path, files: list[Path], query: str, literal: bool, case_sensitive: bool,
                     before_context: int, after_context: int, offset: int, limit: int, deadline: float,
                     *, empty_globs: tuple[str, ...] = ()) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        base = [self.executable, "--no-config", "--json", "--sort=path", "--text", "--encoding=none",
                "--no-follow", f"--max-count={offset + limit}", f"--before-context={before_context}",
                f"--after-context={after_context}"]
        if literal:
            base.append("--fixed-strings")
        if not case_sensitive:
            base.append("--ignore-case")
        if not files:
            # Compile the same native patterns even for an empty candidate
            # set. Depth zero observes no entries and reads no file content.
            base.append("--max-depth=0")
            base.extend(f"--glob={pattern}" for pattern in empty_globs)
        targets = list(map(str, files)) if files else [str(root)]
        result = self._run([*base, "--", query, *targets], root, deadline - time.monotonic())
        if result.returncode not in (0, 1):
            invalid = any(token in result.stderr for token in ("regex parse error", "error parsing glob", "regex error"))
            raise SearchError(result.stderr[:1600] or f"ripgrep exited {result.returncode}",
                              "search_invalid_pattern" if invalid else "search_failed")
        parsed = _parse_events(result.stdout, root, set(files))
        matches, contexts = parsed.matches, parsed.contexts
        if bool(matches) != (result.returncode == 0):
            raise SearchError("ripgrep status differs from observed matches", "search_malformed_output")
        return matches, contexts


def _select_context(matches: list[dict[str, object]], contexts: Iterable[dict[str, object]], before: int, after: int) -> list[dict[str, object]]:
    by_key: dict[tuple[str, int], dict[str, object]] = {}
    selected_matches = {(str(row["path"]), _line_number(row["line"])) for row in matches}
    for row in contexts:
        key = (str(row["path"]), _line_number(row["line"]))
        if key in selected_matches:
            continue
        for match in matches:
            if match["path"] == key[0] and -before <= key[1] - _line_number(match["line"]) <= after:
                by_key[key] = dict(row)
                break
    for row in by_key.values():
        same = sorted(_line_number(match["line"]) for match in matches if match["path"] == row["path"])
        line = _line_number(row["line"])
        row["relation"] = "before" if same and line < min(same) else "after" if same and line > max(same) else "between"
    return sorted(by_key.values(), key=lambda row: (str(row["path"]), _line_number(row["line"])))


def _render(value: dict[str, Any], budget: BoundedScanBudget, scan: Scan, max_file_bytes: int, max_scan_bytes: int) -> ToolOutcome:
    rows: list[tuple[str, int, str, bool]] = []
    for match in value["matches"]:
        rows.append((str(match["path"]), int(match["line"]), str(match["text"]), False))
    for row in value["context"]:
        rows.append((str(row["path"]), int(row["line"]), f"[context {row['relation']}] {row['text']}", True))
    rows.sort(key=lambda item: (item[0], item[1], item[3]))
    lines = [f"{path}:{line}: {text.strip() if len(text.strip()) <= 240 else text.strip()[:237] + '...'}"
             for path, line, text, _ in rows]
    if not lines:
        lines.append("(no matches in admitted files)")
    if value["has_more"] or value["offset"] > 1:
        lines.append(page_footer(query=value["query"], path=value["path"], offset=value["offset"], limit=value["limit"],
            shown=len(value["matches"]), has_more=value["has_more"], literal=value["literal"],
            case_sensitive=value["case_sensitive"], include=value["include"], exclude=value["exclude"],
            before_context=value["before_context"], after_context=value["after_context"]))
    if scan.oversized:
        lines.append(f"... skipped {scan.oversized} file(s) larger than {max_file_bytes} bytes; omitted files may contain more matches")
    if scan.byte_limited:
        lines.append(f"... search scan stopped at {max_scan_bytes} byte read budget; omitted files may contain more matches")
    if scan.unreadable:
        lines.append(f"Scan coverage: could not read metadata for {scan.unreadable} file(s); omitted files may contain more matches")
    if budget.limited:
        lines.append(budget.stop_message("search scan"))
    validate_output("grep", search_output_schema(), value)
    return ToolOutcome("\n".join(lines), True, canonical={"value": value}, truncated=value["has_more"] or not value["scan_complete"],
        presentation={"preview": {"query": value["query"], "matches": len(value["matches"]), "context": len(value["context"]), "has_more": value["has_more"]}})


def search(root: Path, files: Iterable[Path], budget: BoundedScanBudget, *, query: str, path: str,
           offset: int, limit: int, literal: bool, case_sensitive: bool, include: str,
           exclude: tuple[str, ...] | list[str] = (), before_context: int = 0, after_context: int = 0,
           max_file_bytes: int, max_scan_bytes: int) -> ToolOutcome:
    """Admit files once, filter them with rg, then derive all result views."""
    try:
        executable = rg_binary()
        if type(literal) is not bool or type(case_sensitive) is not bool or not isinstance(include, str):
            raise SearchError("literal/case_sensitive must be booleans and include a glob string", "invalid_args")
        if include.startswith("!") or "\x00" in include:
            raise SearchError("include must be one positive glob", "invalid_args")
        if not isinstance(exclude, (tuple, list)) or any(not isinstance(item, str) or not item or item.startswith("!") or "\x00" in item for item in exclude):
            raise SearchError("exclude must be positive glob strings", "invalid_args")
        exclude = tuple(exclude)
        if type(before_context) is not int or before_context < 0 or type(after_context) is not int or after_context < 0:
            raise SearchError("context values must be non-negative integers", "invalid_args")
        if "\x00" in query or "\n" in query or "\r" in query:
            raise SearchError("query must be a single line without NUL", "invalid_args")
        if (include or exclude) and (budget.entry_limited or budget.read_failed):
            raise SearchError("glob filtering requires complete directory entries; narrow the search path",
                              "search_glob_discovery_limit")
        executor = RgExecutor(executable)
        deadline = time.monotonic() + SEARCH_TIMEOUT_SECONDS
        selected = executor.select_files(root.resolve(), list(files), include, exclude, deadline)
        scan = _candidates(selected, max_file_bytes, max_scan_bytes)
        matches: list[dict[str, object]] = []
        contexts: list[dict[str, object]] = []
        seen = 0
        batches = _batches(scan.files, [executable, query]) if scan.files else [[]]
        for batch in batches:
            cancellation.check()
            rows, context_rows = executor.search_batch(root.resolve(), [Path(item) for item in batch], query, literal,
                case_sensitive, before_context, after_context, offset, limit, deadline,
                empty_globs=tuple(([include] if include else []) + [f"!{pattern}" for pattern in exclude]))
            contexts.extend(context_rows)
            # Matching lines outside this page still belong to its requested
            # context window. Their role is context, never another page hit.
            contexts.extend({"path": row["path"], "line": row["line"], "text": row["text"]} for row in rows)
            for row in rows:
                seen += 1
                if seen >= offset:
                    matches.append(row)
                if len(matches) > limit:
                    break
            if len(matches) > limit:
                break
        has_more = len(matches) > limit
        page_matches = matches[:limit]
        page_context = _select_context(page_matches, contexts, before_context, after_context)
        value = {"query": query, "path": path, "literal": literal, "case_sensitive": case_sensitive,
                 "include": include, "exclude": list(exclude), "before_context": before_context,
                 "after_context": after_context, "offset": offset, "limit": limit,
                 "next_offset": offset + limit if has_more else 0, "has_more": has_more,
                 "scan_complete": not (has_more or budget.limited or scan.byte_limited or scan.oversized or scan.unreadable),
                 "matches": page_matches, "context": page_context,
            "coverage": {"files_admitted": len(scan.files), "bytes_admitted": scan.bytes, "oversized": scan.oversized,
                              "unreadable": scan.unreadable, "budget_limited": budget.limited or scan.byte_limited}}
        return _render(value, budget, scan, max_file_bytes, max_scan_bytes)
    except (PackageNotFoundError, FileNotFoundError) as exc:
        return ToolOutcome.error(f"packaged rg is unavailable: {exc}", error_code="search_unavailable")
    except SearchError as exc:
        return ToolOutcome.error(str(exc), error_code=exc.code)
    except subprocess.TimeoutExpired:
        return ToolOutcome.error("ripgrep search timed out", error_code="search_timeout")
    except (OSError, cancellation.PipeDrainTimeout, cancellation.ProcessOutputReadError) as exc:
        return ToolOutcome.error(f"ripgrep search failed: {exc}", error_code="search_failed")


__all__ = ["RgExecutor", "SearchError", "search"]
