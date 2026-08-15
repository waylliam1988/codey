"""Manual A/B for a probe-only Coverage-aware Task Lens.

This does not change production Project Map behavior. It compares:

* current: production ProjectTaskContextBuilder Project Map
* lens: the same bounded Project Map, but with Focused subtree/Symbol overview
  replaced by a compact Task Lens prototype.

The file-pick mode asks a live provider to choose first files. The readonly
mode runs the local agent with write/run tools disabled.
"""

# ruff: noqa: E402 - direct script execution must add the repository root first.

from __future__ import annotations

import argparse
import ast
import json
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codey import provider_controls
from codey.agent import run as run_agent
from codey.agent_tools import AgentToolFns
from codey.bounded_scan import BoundedScanBudget, iter_bounded_files
from codey.project_map import (
    EXCLUDED_DIRS,
    JS_SYMBOL_RE,
    MAX_FOCUS_SCAN_DIRS,
    MAX_FOCUS_SCAN_FILES,
    MAX_FOCUS_SOURCE_BYTES,
    MAX_FOCUS_TOTAL_BYTES,
    MAX_SYMBOLS_PER_FILE,
    SOURCE_SUFFIXES,
    _is_test_path,
    _path_blocked,
    _python_args,
    _safe_relative,
    _symbol_score,
    _tokens,
    build_project_map,
)
from codey.protocols.json_codec import JsonToolCodec
from codey.providers.registry import DEFAULT_PROVIDER_ID, connect_provider, provider_ids
from codey.tool_runtime import ToolOutcome, list_directory
from tests.manual.project_task_context import (
    production_candidate_command_lines,
    render_production_project_map,
)
from tests.manual.zoom_project_map_ab import ProbeCase, build_deep_fixture

DEFAULT_OUTPUT = Path(tempfile.gettempdir()) / "codey-task-lens-ab.json"
ARMS = ("current", "lens")
MODES = ("pick", "readonly")
MAX_RAW_REPLY_CHARS = 3_000
TASK_LENS_TARGET_CHARS = 1_200
TASK_LENS_HARD_CHARS = 1_600
MAX_LIKELY_SOURCE = 5
MAX_LIKELY_TESTS = 3
MAX_LENS_COMMANDS = 3
MIN_LENS_SCORE = 8
GENERIC_NAV_TOKENS = {
    "focused",
    "include",
    "test",
    "testing",
    "tests",
    "verify",
    "verification",
}

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
except (AttributeError, OSError):
    pass


@dataclass(frozen=True)
class TaskLensItem:
    path: str
    role: str
    reason: str
    score: int


@dataclass(frozen=True)
class TaskLensCommand:
    command: str
    reason: str


@dataclass(frozen=True)
class TaskLensOmission:
    kind: str
    count: int
    reason: str


@dataclass(frozen=True)
class TaskLens:
    likely_files: tuple[TaskLensItem, ...] = ()
    likely_tests: tuple[TaskLensItem, ...] = ()
    candidate_commands: tuple[TaskLensCommand, ...] = ()
    omitted: tuple[TaskLensOmission, ...] = ()

    @property
    def has_candidates(self) -> bool:
        return bool(self.likely_files or self.likely_tests)

    def render(self, max_chars: int = TASK_LENS_TARGET_CHARS) -> str:
        if not self.has_candidates:
            return ""
        lines = ["Task Lens (task-scored navigation; read files before editing):"]
        if self.likely_files:
            lines.append("likely_files[path,role,reason]:")
            for item in self.likely_files[:MAX_LIKELY_SOURCE]:
                lines.append(f"  {item.path},{item.role},{item.reason}")
        if self.likely_tests:
            lines.append("likely_tests[path,reason]:")
            for item in self.likely_tests[:MAX_LIKELY_TESTS]:
                lines.append(f"  {item.path},{item.reason}")
        if self.candidate_commands:
            lines.append("candidate_commands[command,reason]:")
            for item in self.candidate_commands[:MAX_LENS_COMMANDS]:
                lines.append(f"  {item.command},{item.reason}")
        if self.omitted:
            lines.append("omitted[kind,count,reason]:")
            for item in self.omitted:
                lines.append(f"  {item.kind},{item.count},{item.reason}")
        lines.append("- not coverage proof; read files before editing")
        return _clip_to_lens_budget(lines, max_chars)


@dataclass
class _LensScanFacts:
    oversized: int = 0
    unreadable: int = 0
    decode_failed: int = 0


class CountingProvider:
    def __init__(self, provider) -> None:
        self.provider = provider
        self.name = provider.name
        self.sends = 0
        self.sent_chars = 0
        self.reply_chars = 0
        self.seconds = 0.0

    def __getattr__(self, name: str):
        return getattr(self.provider, name)

    def send(self, text: str, timeout: float | None = None) -> str:
        self.sends += 1
        self.sent_chars += len(text or "")
        started = time.monotonic()
        reply = self.provider.send(text, timeout=timeout)
        self.seconds += time.monotonic() - started
        self.reply_chars += len(reply or "")
        return reply


def build_task_lens(
    root: Path,
    task: str,
    *,
    candidate_commands: tuple[str, ...] = (),
    max_chars: int = TASK_LENS_TARGET_CHARS,
) -> str:
    lens = _build_task_lens(root, task, candidate_commands=candidate_commands)
    return lens.render(max_chars=max_chars) if lens.has_candidates else ""


def render_current_project_map(root: Path, task: str) -> str:
    return render_production_project_map(root, task=task)


def render_lens_project_map(root: Path, task: str) -> str:
    project_map = build_project_map(
        root,
        task=task,
        candidate_commands=production_candidate_command_lines(root, task=task),
    )
    lens = build_task_lens(
        root,
        task,
        candidate_commands=project_map.candidate_commands[:MAX_LENS_COMMANDS],
    )
    if not lens:
        return project_map.render()
    base = replace(
        project_map,
        candidate_commands=(),
        focused_subtree="",
        symbol_overview="",
    ).render()
    return f"{base}\n{lens}".strip()


def _build_task_lens(
    root: Path,
    task: str,
    *,
    candidate_commands: tuple[str, ...],
) -> TaskLens:
    task = (task or "").strip()
    if not task:
        return TaskLens()
    root = root.expanduser().resolve()
    facts = _LensScanFacts()
    budget = BoundedScanBudget(
        max_files=MAX_FOCUS_SCAN_FILES,
        max_dirs=MAX_FOCUS_SCAN_DIRS,
        max_dir_entries=1_000,
        max_bytes=MAX_FOCUS_TOTAL_BYTES,
    )
    candidates: list[TaskLensItem] = []

    def allow_dir(path: Path) -> bool:
        rel = _safe_relative(root, path)
        return bool(rel and not _path_blocked(rel))

    def allow_file(path: Path) -> bool:
        rel = _safe_relative(root, path)
        if not rel or _path_blocked(rel):
            return False
        if path.suffix.lower() not in SOURCE_SUFFIXES:
            return False
        try:
            size = path.stat().st_size
        except OSError:
            facts.unreadable += 1
            return False
        if size > MAX_FOCUS_SOURCE_BYTES:
            facts.oversized += 1
            return False
        return True

    for path in iter_bounded_files(
        root,
        excluded_dirs=EXCLUDED_DIRS,
        budget=budget,
        allow_dir=allow_dir,
        allow_file=allow_file,
    ):
        rel = _safe_relative(root, path)
        if not rel:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            facts.decode_failed += 1
            continue
        except OSError:
            facts.unreadable += 1
            continue
        symbols = _symbols_from_text(path, text)
        score = _symbol_score(rel, symbols, task)
        if score < MIN_LENS_SCORE:
            continue
        is_test = _is_test_path(rel)
        if is_test and not _has_meaningful_task_match(rel, symbols, task):
            continue
        candidates.append(
            TaskLensItem(
                path=rel,
                role="test" if is_test else "source",
                reason=_reason_for_candidate(rel, symbols, task, is_test=is_test),
                score=score,
            )
        )

    source_items = tuple(
        sorted(
            (item for item in candidates if item.role == "source"),
            key=lambda item: (-item.score, item.path),
        )[:MAX_LIKELY_SOURCE]
    )
    test_items = tuple(
        sorted(
            (item for item in candidates if item.role == "test"),
            key=lambda item: (-item.score, item.path),
        )[:MAX_LIKELY_TESTS]
    )
    commands = tuple(
        TaskLensCommand(command=command, reason="manifest candidate")
        for command in candidate_commands[:MAX_LENS_COMMANDS]
    )
    return TaskLens(
        likely_files=source_items,
        likely_tests=test_items,
        candidate_commands=commands,
        omitted=_omissions_from_scan(facts, budget),
    )


def _symbols_from_text(path: Path, text: str) -> tuple[str, ...]:
    suffix = path.suffix.lower()
    if suffix == ".py":
        return _python_symbols_from_text(text)
    if suffix in {".js", ".jsx", ".ts", ".tsx"}:
        return _js_symbols_from_text(text)
    return ()


def _python_symbols_from_text(text: str) -> tuple[str, ...]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
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


def _js_symbols_from_text(text: str) -> tuple[str, ...]:
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


def _reason_for_candidate(
    rel: str,
    symbols: tuple[str, ...],
    task: str,
    *,
    is_test: bool,
) -> str:
    task_tokens = _meaningful_tokens(task)
    path_hits = sorted((task_tokens & _tokens(rel.replace("/", " "))))[:3]
    symbol_hits = sorted((task_tokens & _tokens(" ".join(symbols))))[:3]
    reasons: list[str] = []
    if path_hits:
        reasons.append("path terms: " + "/".join(path_hits))
    if symbol_hits:
        reasons.append("symbol terms: " + "/".join(symbol_hits))
    if is_test:
        reasons.append("test path")
    if not reasons and symbols:
        reasons.append("symbols: " + _short_symbol_label(symbols[0]))
    return "; ".join(reasons[:3]) if reasons else "task score"


def _has_meaningful_task_match(rel: str, symbols: tuple[str, ...], task: str) -> bool:
    task_tokens = _meaningful_tokens(task)
    if not task_tokens:
        return False
    path_tokens = _tokens(rel.replace("/", " "))
    symbol_tokens = _tokens(" ".join(symbols))
    return bool(task_tokens & path_tokens or task_tokens & symbol_tokens)


def _meaningful_tokens(text: str) -> set[str]:
    return _tokens(text) - GENERIC_NAV_TOKENS


def _short_symbol_label(symbol: str) -> str:
    text = symbol.replace(",", " ").strip()
    return text[:80].rstrip()


def _omissions_from_scan(
    facts: _LensScanFacts,
    budget: BoundedScanBudget,
) -> tuple[TaskLensOmission, ...]:
    omitted: list[TaskLensOmission] = []
    if budget.limited:
        omitted.append(
            TaskLensOmission(
                kind="files",
                count=1,
                reason="scan budget reached; relevant files may be omitted",
            )
        )
    skipped = facts.oversized + facts.decode_failed + facts.unreadable
    if skipped:
        reasons: list[str] = []
        if facts.oversized:
            reasons.append("oversized")
        if facts.decode_failed:
            reasons.append("non-UTF-8")
        if facts.unreadable:
            reasons.append("unreadable")
        omitted.append(
            TaskLensOmission(
                kind="files",
                count=skipped,
                reason="/".join(reasons) + "; relevant files may be omitted",
            )
        )
    return tuple(omitted)


def _clip_to_lens_budget(lines: list[str], max_chars: int) -> str:
    kept: list[str] = []
    for line in lines:
        candidate = "\n".join([*kept, line])
        if len(candidate) > max_chars:
            break
        kept.append(line)
    if len(kept) < len(lines):
        marker = "- task lens truncated"
        while kept and len("\n".join([*kept, marker])) > TASK_LENS_HARD_CHARS:
            kept.pop()
        kept.append(marker)
    rendered = "\n".join(kept)
    if len(rendered) <= TASK_LENS_HARD_CHARS:
        return rendered
    return rendered[:TASK_LENS_HARD_CHARS].rstrip() + "\n- task lens truncated"


def _clip(value: object, limit: int = MAX_RAW_REPLY_CHARS) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n[truncated]"


def _extract_json_object(text: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    for index, char in enumerate(text or ""):
        if char != "{":
            continue
        try:
            value, _end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return {}


def _normalize_path(value: object) -> str:
    text = str(value or "").replace("\\", "/").strip().strip("\"'")
    while text.startswith("./"):
        text = text[2:]
    return text.strip("/")


def _coerce_path_list(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        raw_items: list[object] = [value]
    elif isinstance(value, list):
        raw_items = value
    else:
        return ()
    paths: list[str] = []
    for item in raw_items:
        path = _normalize_path(item)
        if path and path not in paths:
            paths.append(path)
    return tuple(paths)


def _paths_from_reply(text: str) -> tuple[str, ...]:
    obj = _extract_json_object(text)
    return _coerce_path_list(obj.get("paths"))


def _test_paths_from_reply(text: str) -> tuple[str, ...]:
    obj = _extract_json_object(text)
    return _coerce_path_list(obj.get("test_paths") or obj.get("tests"))


def _path_matches(actual: str, expected: str) -> bool:
    actual = _normalize_path(actual)
    expected = _normalize_path(expected)
    return (
        actual == expected
        or actual.endswith("/" + expected)
        or expected.endswith("/" + actual)
    )


def _score_paths(paths: tuple[str, ...], expected: tuple[str, ...]) -> dict[str, Any]:
    hits: list[str] = []
    first_expected_rank = None
    for expected_path in expected:
        if any(_path_matches(path, expected_path) for path in paths):
            hits.append(expected_path)
    for index, path in enumerate(paths, start=1):
        if any(_path_matches(path, expected_path) for expected_path in expected):
            first_expected_rank = index
            break
    return {
        "hits": hits,
        "hit_count": len(hits),
        "first_expected_rank": first_expected_rank,
        "top1_hit": first_expected_rank == 1,
        "top3_hit": first_expected_rank is not None and first_expected_rank <= 3,
    }


def _score_selection(
    case: ProbeCase,
    paths: tuple[str, ...],
    test_paths: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "paths": _score_paths(paths, case.expected_paths),
        "tests": _score_paths(test_paths, case.expected_tests),
    }


def _summary_path_hits(text: str, expected: tuple[str, ...]) -> list[str]:
    lower = (text or "").lower().replace("\\", "/")
    hits = []
    for path in expected:
        normalized = path.lower().replace("\\", "/")
        basename = PurePosixPath(normalized).name
        if normalized in lower or basename in lower:
            hits.append(path)
    return hits


def _selection_prompt(case: ProbeCase, root: Path, *, arm: str) -> str:
    map_text = _map_for_arm(root, case.task, arm=arm)
    listing = list_directory(root, ".").model_text
    return "\n".join(
        [
            "You are helping evaluate a local coding agent's project navigation.",
            "Do not solve the code change. Only choose files to inspect first.",
            "Prefer exact relative file paths from the map. Do not invent paths.",
            "Return exactly one JSON object, no markdown:",
            (
                '{"paths":["relative/implementation.py"],'
                '"test_paths":["relative/test_file.py"],'
                '"reason":"short reason"}'
            ),
            "",
            f"Project root: {root}",
            f"Map arm: {arm}",
            "Initial listing:",
            listing,
            "",
            map_text,
            "",
            "Task:",
            case.task,
        ]
    )


def _readonly_task(case: ProbeCase) -> str:
    return "\n".join(
        [
            "Read-only navigation benchmark.",
            "Use read_file before answering. Do not edit files and do not run commands.",
            "Answer with the relevant relative file paths and function/class names.",
            case.task,
        ]
    )


def _map_for_arm(root: Path, task: str, *, arm: str) -> str:
    if arm == "current":
        return render_current_project_map(root, task)
    if arm == "lens":
        return render_lens_project_map(root, task)
    raise ValueError(f"unknown arm: {arm}")


def _fresh_chat(provider: CountingProvider) -> None:
    new_chat = getattr(provider, "new_chat", None)
    if callable(new_chat):
        new_chat()


def run_pick_arm(
    provider: CountingProvider,
    case: ProbeCase,
    root: Path,
    arm: str,
    *,
    timeout: float,
    fresh_chat: bool,
) -> dict[str, Any]:
    if fresh_chat:
        _fresh_chat(provider)
    sent_before = provider.sent_chars
    reply_before = provider.reply_chars
    sends_before = provider.sends
    seconds_before = provider.seconds
    started = time.monotonic()

    prompt = _selection_prompt(case, root, arm=arm)
    reply = provider.send(prompt, timeout=timeout)
    paths = _paths_from_reply(reply)
    test_paths = _test_paths_from_reply(reply)
    score = _score_selection(case, paths, test_paths)
    return {
        "mode": "pick",
        "case": case.name,
        "arm": arm,
        "tags": list(case.tags),
        "target_named_in_task": case.target_named_in_task,
        "task": case.task,
        "expected_paths": list(case.expected_paths),
        "expected_tests": list(case.expected_tests),
        "paths": list(paths),
        "test_paths": list(test_paths),
        "score": score,
        "ok": score["paths"]["hit_count"] > 0 or score["tests"]["hit_count"] > 0,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "provider_seconds": round(provider.seconds - seconds_before, 3),
        "sends": provider.sends - sends_before,
        "sent_chars": provider.sent_chars - sent_before,
        "reply_chars": provider.reply_chars - reply_before,
        "prompt_chars": len(prompt),
        "raw_reply": _clip(reply),
    }


def _read_only_error(*_args, **_kwargs) -> ToolOutcome:
    return ToolOutcome.error("A/B benchmark is read-only; edit and run are disabled.")


def run_readonly_arm(
    provider_id: str,
    case: ProbeCase,
    root: Path,
    arm: str,
    *,
    port: int,
    max_turns: int,
) -> dict[str, Any]:
    tool_fns = AgentToolFns(
        write_file=_read_only_error,
        edit_file=_read_only_error,
        run_command=_read_only_error,
    )
    raw = None
    events = []
    started = time.monotonic()
    try:
        raw = connect_provider(provider_id, port=port)
        provider = CountingProvider(raw)
        provider_controls.begin_task_context(f"task-lens-ab-readonly:{provider_id}:{case.name}:{arm}")
        result = run_agent(
            provider,
            root,
            _readonly_task(case),
            codec=JsonToolCodec(),
            max_turns=max_turns,
            on_event=events.append,
            fresh_chat=True,
            project_map=_map_for_arm(root, case.task, arm=arm),
            tool_fns=tool_fns,
        )
        tool_events = [
            event
            for event in events
            if event.kind == "tool" and event.call is not None and event.outcome is not None
        ]
        counts = Counter(event.call.name for event in tool_events)
        first_read_path = ""
        for event in tool_events:
            if event.call.name in {"read", "read_file"}:
                first_read_path = _normalize_path(event.call.args.get("path"))
                break
        expected_all = (*case.expected_paths, *case.expected_tests)
        summary_path_hits = _summary_path_hits(result.summary, expected_all)
        first_read_hit = bool(
            first_read_path
            and any(_path_matches(first_read_path, expected) for expected in expected_all)
        )
        trace = [
            {
                "turn": event.turn,
                "tool": event.call.name,
                "args": event.call.args,
                "ok": event.outcome.ok,
                "truncated": event.outcome.truncated,
                "output_chars": len(event.outcome.model_text),
            }
            for event in tool_events
        ]
        return {
            "mode": "readonly",
            "case": case.name,
            "arm": arm,
            "provider": provider_id,
            "tags": list(case.tags),
            "target_named_in_task": case.target_named_in_task,
            "task": case.task,
            "expected_paths": list(case.expected_paths),
            "expected_tests": list(case.expected_tests),
            "ok": result.stop_reason == "done" and not result.changed,
            "correct_answer": bool(summary_path_hits),
            "summary_path_hits": summary_path_hits,
            "first_read_path": first_read_path,
            "first_read_hit": first_read_hit,
            "stop_reason": result.stop_reason,
            "turns": result.turns,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "provider_seconds": round(provider.seconds, 3),
            "sends": provider.sends,
            "sent_chars": provider.sent_chars,
            "reply_chars": provider.reply_chars,
            "tool_counts": dict(sorted(counts.items())),
            "tool_calls": len(tool_events),
            "search_calls": counts.get("search", 0) + counts.get("search_files", 0),
            "references_calls": counts.get("find_references", 0),
            "trace": trace,
            "summary": result.summary,
        }
    finally:
        try:
            if raw is not None:
                raw.close()
        finally:
            provider_controls.end_task_context()


def _arm_order(order: str) -> tuple[str, ...]:
    if order == "lens-first":
        return ("lens", "current")
    if order == "current-first":
        return ("current", "lens")
    raise ValueError(f"unknown order: {order}")


def _row_status(row: dict[str, Any]) -> str:
    if "error" in row:
        return "ERR"
    if row.get("mode") == "readonly":
        return (
            f"correct={int(bool(row.get('correct_answer')))} "
            f"first_read={int(bool(row.get('first_read_hit')))} "
            f"tools={row.get('tool_calls')} turns={row.get('turns')}"
        )
    score = row["score"]
    return (
        f"paths={score['paths']['hit_count']} "
        f"tests={score['tests']['hit_count']} "
        f"top1={int(score['paths']['top1_hit'])} "
        f"top3={int(score['paths']['top3_hit'])} "
        f"chars={row['prompt_chars']}"
    )


def run_pick_provider(
    provider_id: str,
    selected_cases: tuple[ProbeCase, ...],
    root: Path,
    *,
    port: int,
    timeout: float,
    order: str,
    fresh_chat: bool,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    raw = None
    provider_controls.begin_task_context(f"task-lens-ab-pick:{provider_id}")
    try:
        raw = connect_provider(provider_id, port=port)
        provider = CountingProvider(raw)
        for case in selected_cases:
            for arm in _arm_order(order):
                try:
                    row = run_pick_arm(
                        provider,
                        case,
                        root,
                        arm,
                        timeout=timeout,
                        fresh_chat=fresh_chat,
                    )
                    row["provider"] = provider_id
                except Exception as exc:
                    row = {
                        "mode": "pick",
                        "provider": provider_id,
                        "case": case.name,
                        "arm": arm,
                        "error": str(exc),
                    }
                rows.append(row)
                print(f"[{provider_id}] pick {case.name} {arm}: {_row_status(row)}", flush=True)
    finally:
        try:
            if raw is not None:
                raw.close()
        finally:
            provider_controls.end_task_context()
    return {
        "provider": provider_id,
        "mode": "pick",
        "rows": rows,
        "summary": _summarize_rows(rows),
    }


def run_readonly_provider(
    provider_id: str,
    selected_cases: tuple[ProbeCase, ...],
    root: Path,
    *,
    port: int,
    order: str,
    max_turns: int,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for case in selected_cases:
        for arm in _arm_order(order):
            try:
                row = run_readonly_arm(
                    provider_id,
                    case,
                    root,
                    arm,
                    port=port,
                    max_turns=max_turns,
                )
            except Exception as exc:
                row = {
                    "mode": "readonly",
                    "provider": provider_id,
                    "case": case.name,
                    "arm": arm,
                    "error": str(exc),
                }
            rows.append(row)
            print(f"[{provider_id}] readonly {case.name} {arm}: {_row_status(row)}", flush=True)
    return {
        "provider": provider_id,
        "mode": "readonly",
        "rows": rows,
        "summary": _summarize_rows(rows),
    }


def _summarize_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    completed = [row for row in rows if "error" not in row]
    summary: dict[str, Any] = {
        "completed_rows": len(completed),
        "errors": len(rows) - len(completed),
        "by_arm": {},
        "deltas_vs_current": {},
    }
    for arm in ARMS:
        arm_rows = [row for row in completed if row.get("arm") == arm]
        pick_rows = [row for row in arm_rows if row.get("mode") == "pick"]
        readonly_rows = [row for row in arm_rows if row.get("mode") == "readonly"]
        summary["by_arm"][arm] = {
            "rows": len(arm_rows),
            "pick_rows": len(pick_rows),
            "readonly_rows": len(readonly_rows),
            "ok": sum(1 for row in arm_rows if row.get("ok")),
            "path_hits": sum(row["score"]["paths"]["hit_count"] for row in pick_rows),
            "test_hits": sum(row["score"]["tests"]["hit_count"] for row in pick_rows),
            "top1_path_hits": sum(
                1 for row in pick_rows if row["score"]["paths"]["top1_hit"]
            ),
            "top3_path_hits": sum(
                1 for row in pick_rows if row["score"]["paths"]["top3_hit"]
            ),
            "readonly_correct": sum(1 for row in readonly_rows if row.get("correct_answer")),
            "first_read_hits": sum(1 for row in readonly_rows if row.get("first_read_hit")),
            "tool_calls": sum(int(row.get("tool_calls") or 0) for row in readonly_rows),
            "search_calls": sum(int(row.get("search_calls") or 0) for row in readonly_rows),
            "references_calls": sum(
                int(row.get("references_calls") or 0) for row in readonly_rows
            ),
            "turns": sum(int(row.get("turns") or 0) for row in readonly_rows),
            "sent_chars": sum(int(row.get("sent_chars") or 0) for row in arm_rows),
            "prompt_chars": sum(int(row.get("prompt_chars") or 0) for row in pick_rows),
            "provider_seconds": round(
                sum(float(row.get("provider_seconds") or 0.0) for row in arm_rows),
                3,
            ),
        }
    current = summary["by_arm"]["current"]
    lens = summary["by_arm"]["lens"]
    delta: dict[str, Any] = {}
    for key in (
        "ok",
        "path_hits",
        "test_hits",
        "top1_path_hits",
        "top3_path_hits",
        "readonly_correct",
        "first_read_hits",
        "tool_calls",
        "search_calls",
        "references_calls",
        "turns",
        "sent_chars",
        "prompt_chars",
    ):
        delta[key] = lens[key] - current[key]
    delta["provider_seconds"] = round(lens["provider_seconds"] - current["provider_seconds"], 3)
    summary["deltas_vs_current"]["lens"] = delta
    return summary


def _summary_with_subsets(rows: list[dict[str, Any]]) -> dict[str, Any]:
    unnamed = [
        row
        for row in rows
        if "error" not in row
        and not row.get("target_named_in_task")
        and "deep" in row.get("tags", [])
    ]
    named = [
        row
        for row in rows
        if "error" not in row
        and row.get("target_named_in_task")
        and "deep" in row.get("tags", [])
    ]
    return {
        "all": _summarize_rows(rows),
        "unnamed_deep": _summarize_rows(unnamed),
        "named_controls": _summarize_rows(named),
    }


def _write_report(
    output: Path,
    *,
    providers: tuple[str, ...],
    modes: tuple[str, ...],
    fixture_root: Path,
    selected_cases: tuple[ProbeCase, ...],
    provider_reports: list[dict[str, Any]],
    partial: bool,
) -> dict[str, Any]:
    rows = [row for report in provider_reports for row in report.get("rows", [])]
    report = {
        "probe": "task_lens_ab",
        "providers": list(providers),
        "completed_reports": [
            f"{report.get('provider')}:{report.get('mode')}" for report in provider_reports
        ],
        "modes": list(modes),
        "partial": partial,
        "fixture_root": fixture_root.as_posix(),
        "case_count": len(selected_cases),
        "cases": [
            {
                "name": case.name,
                "target_named_in_task": case.target_named_in_task,
                "tags": list(case.tags),
                "expected_paths": list(case.expected_paths),
                "expected_tests": list(case.expected_tests),
            }
            for case in selected_cases
        ],
        "provider_reports": provider_reports,
        "summary": _summary_with_subsets(rows),
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text + "\n", encoding="utf-8")
    return report


def _prepare_fixture(args: argparse.Namespace):
    if args.fixture_root is not None:
        root = args.fixture_root.expanduser().resolve()
        cases = build_deep_fixture(root)
        return root, cases, None
    manager = tempfile.TemporaryDirectory(prefix="codey-task-lens-fixture-")
    root = Path(manager.name)
    cases = build_deep_fixture(root)
    return root, cases, manager


def run_self_test() -> None:
    with tempfile.TemporaryDirectory(prefix="codey-task-lens-selftest-") as td:
        root = Path(td)
        available = build_deep_fixture(root)
        case = available["billing-proration"]
        current = render_current_project_map(root, case.task)
        lens = render_lens_project_map(root, case.task)
        lens_block = build_task_lens(root, case.task)
        assert "Focused subtree" in current, current
        assert "Task Lens" in lens, lens
        assert "Focused subtree" not in lens, lens
        assert "Symbol overview" not in lens, lens
        assert len(lens_block) <= TASK_LENS_HARD_CHARS, len(lens_block)
        assert case.expected_paths[0] in lens_block, lens_block
        assert case.expected_tests[0] in lens_block, lens_block
        prompt = _selection_prompt(case, root, arm="lens")
        assert "Map arm: lens" in prompt
        assert "Task Lens" in prompt

        (root / "src").mkdir(exist_ok=True)
        (root / "src" / "binary_router.py").write_bytes(b"\xff\xfe\x00")
        (root / "src" / "big_router.py").write_text(
            "def big_router_dispatch():\n    return True\n"
            + ("#" * (MAX_FOCUS_SOURCE_BYTES + 1)),
            encoding="utf-8",
        )
        (root / ".env").write_text("SECRET=1\n", encoding="utf-8")
        secret_lens = build_task_lens(root, case.task)
        assert "omitted[kind,count,reason]" in secret_lens, secret_lens
        assert "non-UTF-8" in secret_lens, secret_lens
        assert "oversized" in secret_lens, secret_lens
        assert ".env" not in secret_lens, secret_lens
        assert "SECRET=1" not in secret_lens, secret_lens

        parsed = _paths_from_reply('{"paths":["./proration_policy.py"]}')
        assert parsed == ("proration_policy.py",)
        score = _score_paths(("proration_policy.py",), case.expected_paths)
        assert score["top1_hit"], score
        rows = [
            {
                "mode": "pick",
                "arm": "current",
                "target_named_in_task": False,
                "tags": ["deep"],
                "ok": True,
                "score": {
                    "paths": {"hit_count": 1, "top1_hit": False, "top3_hit": True},
                    "tests": {"hit_count": 0},
                },
                "sent_chars": 100,
                "prompt_chars": 100,
                "provider_seconds": 1.0,
            },
            {
                "mode": "pick",
                "arm": "lens",
                "target_named_in_task": False,
                "tags": ["deep"],
                "ok": True,
                "score": {
                    "paths": {"hit_count": 2, "top1_hit": True, "top3_hit": True},
                    "tests": {"hit_count": 1},
                },
                "sent_chars": 90,
                "prompt_chars": 90,
                "provider_seconds": 1.2,
            },
        ]
        summary = _summary_with_subsets(rows)
        delta = summary["unnamed_deep"]["deltas_vs_current"]["lens"]
        assert delta["path_hits"] == 1, delta
        assert delta["prompt_chars"] == -10, delta
    print("self-test passed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare current Project Map with a probe-only Task Lens."
    )
    parser.add_argument("--provider", choices=(*provider_ids(), "all"), default=DEFAULT_PROVIDER_ID)
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--case", action="append")
    parser.add_argument("--max-cases", type=int, default=0)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--max-turns", type=int, default=10)
    parser.add_argument("--mode", choices=(*MODES, "both"), default="pick")
    parser.add_argument(
        "--order",
        choices=("current-first", "lens-first"),
        default="current-first",
    )
    parser.add_argument(
        "--reuse-chat",
        action="store_true",
        help="Do not open a fresh provider chat before each pick arm.",
    )
    parser.add_argument("--fixture-root", type=Path)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--print-maps", action="store_true")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build fixture and optional maps, then exit before opening providers.",
    )
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)

    if args.self_test:
        run_self_test()
        return 0

    root, available, manager = _prepare_fixture(args)
    try:
        if args.case:
            missing = [name for name in args.case if name not in available]
            if missing:
                parser.error(f"unknown case(s): {', '.join(missing)}")
            selected = tuple(available[name] for name in args.case)
        else:
            selected = tuple(available.values())
        if args.max_cases > 0:
            selected = selected[: args.max_cases]
        if not selected:
            parser.error("no probe cases selected")

        if args.print_maps:
            for case in selected:
                current = render_current_project_map(root, case.task)
                lens = render_lens_project_map(root, case.task)
                print(
                    f"[{case.name}] current_chars={len(current)} "
                    f"lens_chars={len(lens)} delta={len(lens) - len(current)}"
                )
                print(lens)
        if args.dry_run:
            return 0

        selected_providers = provider_ids() if args.provider == "all" else (args.provider,)
        selected_modes = MODES if args.mode == "both" else (args.mode,)
        reports: list[dict[str, Any]] = []
        total_reports = len(selected_providers) * len(selected_modes)
        for provider_id in selected_providers:
            for mode in selected_modes:
                if mode == "pick":
                    reports.append(
                        run_pick_provider(
                            provider_id,
                            selected,
                            root,
                            port=args.port,
                            timeout=args.timeout,
                            order=args.order,
                            fresh_chat=not args.reuse_chat,
                        )
                    )
                elif mode == "readonly":
                    reports.append(
                        run_readonly_provider(
                            provider_id,
                            selected,
                            root,
                            port=args.port,
                            order=args.order,
                            max_turns=args.max_turns,
                        )
                    )
                _write_report(
                    args.output,
                    providers=selected_providers,
                    modes=selected_modes,
                    fixture_root=root,
                    selected_cases=selected,
                    provider_reports=reports,
                    partial=len(reports) < total_reports,
                )
        report = _write_report(
            args.output,
            providers=selected_providers,
            modes=selected_modes,
            fixture_root=root,
            selected_cases=selected,
            provider_reports=reports,
            partial=False,
        )
        print(json.dumps(report["summary"], ensure_ascii=False, indent=2), flush=True)
        return 0
    finally:
        if manager is not None:
            manager.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
