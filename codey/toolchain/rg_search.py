"""One packaged-ripgrep search engine; callers supply their authorized file set."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path, PurePosixPath
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


def _candidates(root: Path, files: Iterable[Path], max_file_bytes: int, max_scan_bytes: int, include: str) -> Scan:
    scan = Scan()
    for path in files:
        cancellation.check()
        try:
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                continue
            relative = PurePosixPath(path.relative_to(root).as_posix())
            if include and not relative.match(include):
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
            scan.files.append(path)
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


def _match(data: Any, root: Path, allowed: set[Path]) -> dict[str, object]:
    try:
        path_text, text = data["path"]["text"], data["lines"]["text"]
        line = data["line_number"]
        parts = data["submatches"]
        if not isinstance(path_text, str) or not isinstance(text, str):
            raise ValueError("path and line must be UTF-8 text")
        path = Path(path_text)
        if not path.is_absolute():
            path = root / path
        if path not in allowed:
            raise ValueError("result path was not admitted")
        relative = path.resolve().relative_to(root).as_posix()
        if type(line) is not int or line < 1 or not isinstance(parts, list) or not parts:
            raise ValueError("invalid match coordinates")
        start = parts[0]["start"]
        if type(start) is not int or start < 0 or start > len(text.encode("utf-8")):
            raise ValueError("invalid match byte column")
        # Remove only the actual line terminator; preserve indentation/content.
        if text.endswith("\n"):
            text = text[:-1].removesuffix("\r")
        return {"path": relative, "line": line, "column_byte": start + 1, "text": text}
    except KeyError as exc:
        if isinstance(data, dict) and isinstance(data.get("lines"), dict) and "bytes" in data["lines"]:
            raise SearchError("matching line is not UTF-8 text; search is incomplete", "search_invalid_encoding") from exc
        raise SearchError("malformed ripgrep match", "search_malformed_output") from exc
    except (ValueError, TypeError, IndexError, OSError) as exc:
        raise SearchError(f"malformed ripgrep match: {exc}", "search_malformed_output") from exc


def _parse(output: str, root: Path, allowed: set[Path]) -> list[dict[str, object]]:
    matches = []
    summary = False
    try:
        for line in output.split("\n"):
            if not line:
                continue
            row = json.loads(line)
            if summary or not isinstance(row, dict) or row.get("type") not in {"begin", "end", "match", "summary"}:
                raise ValueError("unknown ripgrep event")
            if not isinstance(row.get("data"), dict):
                raise ValueError("missing ripgrep event data")
            if row["type"] == "match":
                matches.append(_match(row["data"], root, allowed))
            if row["type"] == "summary":
                stats = row["data"].get("stats")
                if (not isinstance(stats, dict) or type(stats.get("matched_lines")) is not int
                        or stats["matched_lines"] != len(matches)):
                    raise ValueError("ripgrep summary differs from observed matches")
                summary = True
        if not summary:
            raise ValueError("missing ripgrep summary")
    except (ValueError, TypeError, KeyError) as exc:
        if isinstance(exc, SearchError):
            raise
        raise SearchError(f"malformed ripgrep output: {exc}", "search_malformed_output") from exc
    return matches


def _run(args: list[str], root: Path, allowed: set[Path], timeout: float) -> list[dict[str, object]]:
    result = cancellation.run_process(args, cwd=root, timeout=max(0.0, timeout),
                                      capture_limit_bytes=SEARCH_RAW_BYTES)
    if result.stdout_truncated or result.stderr_truncated:
        raise SearchError("ripgrep raw output exceeded its budget; narrow the search", "search_output_limit")
    if result.returncode not in (0, 1):
        invalid = any(s in result.stderr for s in ("regex parse error", "error parsing glob", "regex error"))
        raise SearchError(result.stderr[:1600] or f"ripgrep exited {result.returncode}",
                          "search_invalid_pattern" if invalid else "search_failed")
    matches = _parse(result.stdout, root, allowed)
    if bool(matches) != (result.returncode == 0):
        raise SearchError("ripgrep status differs from observed matches", "search_malformed_output")
    return matches


def _render(value: dict[str, Any], budget: BoundedScanBudget, scan: Scan, max_file_bytes: int,
            max_scan_bytes: int) -> ToolOutcome:
    lines = [f"{m['path']}:{m['line']}: " + (m['text'].strip() if len(m['text'].strip()) <= 240
             else m['text'].strip()[:237] + "...") for m in value["matches"]]
    if not lines:
        lines.append("(no matches in admitted files)")
    if value["has_more"] or value["offset"] > 1:
        lines.append(page_footer(query=value["query"], path=value["path"], offset=value["offset"],
            limit=value["limit"], shown=len(value["matches"]), has_more=value["has_more"],
            literal=value["literal"], case_sensitive=value["case_sensitive"], include=value["include"]))
    if scan.oversized:
        lines.append(f"... skipped {scan.oversized} file(s) larger than {max_file_bytes} bytes; omitted files may contain more matches")
    if scan.byte_limited:
        lines.append(f"... search scan stopped at {max_scan_bytes} byte read budget; omitted files may contain more matches")
    if scan.unreadable:
        lines.append(f"Scan coverage: could not read metadata for {scan.unreadable} file(s); omitted files may contain more matches")
    if budget.limited:
        lines.append(budget.stop_message("search scan"))
    validate_output("grep", search_output_schema(), value)
    return ToolOutcome("\n".join(lines), True, canonical={"value": value},
        truncated=value["has_more"] or not value["scan_complete"],
        presentation={"preview": {"query": value["query"], "matches": len(value["matches"]), "has_more": value["has_more"]}})


def search(root: Path, files: Iterable[Path], budget: BoundedScanBudget, *, query: str, path: str,
           offset: int, limit: int, literal: bool, case_sensitive: bool, include: str,
           max_file_bytes: int, max_scan_bytes: int) -> ToolOutcome:
    """Admit files once, execute rg through the existing process owner, derive views."""
    try:
        executable = rg_binary()
        if type(literal) is not bool or type(case_sensitive) is not bool or not isinstance(include, str):
            raise SearchError("literal/case_sensitive must be booleans and include a glob string", "invalid_args")
        if include.startswith("!") or "\x00" in include:
            raise SearchError("include must be one positive glob", "invalid_args")
        if "\x00" in query or "\n" in query or "\r" in query:
            raise SearchError("query must be a single line without NUL", "invalid_args")
        scan = _candidates(root, files, max_file_bytes, max_scan_bytes, include)
        base = [executable, "--no-config", "--json", "--sort=path", "--text", "--encoding=none", "--no-follow",
                f"--max-count={offset + limit}"]
        if literal:
            base.append("--fixed-strings")
        if not case_sensitive:
            base.append("--ignore-case")
        base.extend(("--", query))
        matches: list[dict[str, object]] = []
        seen = 0
        deadline = time.monotonic() + SEARCH_TIMEOUT_SECONDS
        for batch in _batches(scan.files, base):
            cancellation.check()
            rows = _run([*base, *batch], root, set(map(Path, batch)), deadline - time.monotonic())
            for row in rows:
                seen += 1
                if seen >= offset:
                    matches.append(row)
                if len(matches) > limit:
                    break
            if len(matches) > limit:
                break
        has_more = len(matches) > limit
        value = {"query": query, "path": path, "literal": literal, "case_sensitive": case_sensitive,
                 "include": include, "offset": offset, "limit": limit, "next_offset": offset + limit if has_more else 0,
                 "has_more": has_more, "scan_complete": not (has_more or budget.limited or scan.byte_limited or scan.oversized or scan.unreadable),
                 "matches": matches[:limit],
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
