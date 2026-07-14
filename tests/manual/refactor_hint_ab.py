"""Live A/B for a narrow incomplete-refactor hint.

This probe does not change production behavior. The hint arm monkeypatches
successful replacement edits so that, after a narrow identifier rename, it
performs a bounded lexical scan for the old name in other source files and
appends one factual note to the tool result.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from codey import agent, provider_controls
from codey.bounded_scan import BoundedScanBudget, iter_bounded_files
from codey.events import RunEvent, render_run_event
from codey.project_map import render_project_map
from codey.providers.registry import connect_provider, provider_ids
from codey.tool_runtime import SEARCH_EXCLUDED_DIRS, ToolOutcome


ARMS = ("baseline", "hint")
IDENTIFIER_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")
SOURCE_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx"}
MIN_SYMBOL_LEN = 5
MAX_SCAN_FILE_BYTES = 256 * 1024
MAX_SCAN_TOTAL_BYTES = 2 * 1024 * 1024
MAX_SCAN_FILES = 800
MAX_SCAN_DIRS = 200
MAX_SCAN_DIR_ENTRIES = 800
GENERIC_SYMBOLS = {
    "config",
    "count",
    "data",
    "false",
    "index",
    "result",
    "state",
    "true",
    "value",
}


@dataclass(frozen=True)
class ProbeCase:
    name: str
    files: dict[str, str]
    task: str
    check: Callable[[Path], bool]
    missed: Callable[[Path], int]
    wrong_extra_edits: Callable[[Path], int]


def _is_interesting_identifier(value: str) -> bool:
    return (
        len(value) >= MIN_SYMBOL_LEN
        and IDENTIFIER_RE.fullmatch(value) is not None
        and value.lower() not in GENERIC_SYMBOLS
    )


def _rename_pair(search: str, replace: str) -> tuple[str, str] | None:
    old_tokens = {
        token
        for token in IDENTIFIER_RE.findall(search)
        if _is_interesting_identifier(token)
    }
    new_tokens = {
        token
        for token in IDENTIFIER_RE.findall(replace)
        if _is_interesting_identifier(token)
    }
    removed = old_tokens - new_tokens
    added = new_tokens - old_tokens
    if len(removed) != 1 or len(added) != 1:
        return None
    old = next(iter(removed))
    new = next(iter(added))
    if old == new:
        return None
    return old, new


def _candidate_pairs(blocks: list[Any]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for block in blocks:
        pair = _rename_pair(str(block.search), str(block.replace))
        if pair is None or pair in seen:
            continue
        seen.add(pair)
        pairs.append(pair)
    return pairs


def _symbol_pattern(symbol: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z0-9_]){re.escape(symbol)}(?![A-Za-z0-9_])")


def _count_other_source_files(root: Path, current_rel: str, symbol: str) -> tuple[int, bool]:
    resolved_root = root.resolve()
    current = (root / current_rel).resolve()
    pattern = _symbol_pattern(symbol)
    count = 0
    bytes_read = 0
    byte_limited = False
    budget = BoundedScanBudget(
        max_files=MAX_SCAN_FILES,
        max_dirs=MAX_SCAN_DIRS,
        max_dir_entries=MAX_SCAN_DIR_ENTRIES,
    )
    for path in iter_bounded_files(
        resolved_root,
        excluded_dirs=SEARCH_EXCLUDED_DIRS,
        budget=budget,
        allow_file=lambda candidate: candidate.suffix.lower() in SOURCE_SUFFIXES,
        skip_start_if_excluded=False,
    ):
        try:
            if path.resolve() == current:
                continue
            size = path.stat().st_size
            if size > MAX_SCAN_FILE_BYTES:
                continue
            if bytes_read + size > MAX_SCAN_TOTAL_BYTES:
                byte_limited = True
                break
            bytes_read += size
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if pattern.search(text):
            count += 1
    return count, budget.limited or byte_limited


def _render_refactor_hint(root: Path, rel: str, blocks: list[Any]) -> str:
    notes: list[str] = []
    for old, new in _candidate_pairs(blocks):
        count, limited = _count_other_source_files(root, rel, old)
        if count <= 0:
            continue
        qualifier = "at least " if limited else ""
        file_word = "file" if count == 1 else "files"
        notes.append(
            f"Note: '{old}' still appears in {qualifier}{count} other {file_word}. "
            "Use find_references before completion if this was intended as a rename."
        )
    return "\n".join(notes)


class _RefactorHintProbe:
    def __init__(self, *, arm: str) -> None:
        self.arm = arm
        self.original = agent.edit_file
        self.generated_hints = 0
        self.exposed_hints = 0

    def __call__(self, root: Path, rel: str, blocks: list[Any]) -> ToolOutcome:
        outcome = self.original(root, rel, blocks)
        if not (outcome.ok and outcome.changed):
            return outcome
        hint = _render_refactor_hint(root, rel, blocks)
        if hint:
            self.generated_hints += 1
        if self.arm != "hint" or not hint:
            return outcome
        self.exposed_hints += 1
        return ToolOutcome(
            output=f"{outcome.output}\n{hint}",
            ok=outcome.ok,
            exit_code=outcome.exit_code,
            changed=outcome.changed,
            truncated=outcome.truncated,
        )


def _write_project(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _run_unittest(root: Path) -> bool:
    completed = subprocess.run(
        (sys.executable, "-B", "-m", "unittest"),
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
    )
    return completed.returncode == 0


def _text(root: Path, rel: str) -> str:
    path = root / rel
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _contains_symbol(root: Path, rel: str, symbol: str) -> bool:
    return _symbol_pattern(symbol).search(_text(root, rel)) is not None


def _symbol_occurrences(root: Path, rel: str, symbol: str) -> int:
    return len(_symbol_pattern(symbol).findall(_text(root, rel)))


def _function_case() -> ProbeCase:
    files = {
        "math_ops.py": (
            "def normalize_value(raw):\n"
            "    return max(0, min(100, raw))\n"
        ),
        "pipeline.py": (
            "from math_ops import normalize_value\n\n"
            "def prepare(raw):\n"
            "    return normalize_value(raw) + 1\n"
        ),
        "test_pipeline.py": (
            "import unittest\n"
            "from pipeline import prepare\n\n"
            "class PipelineTests(unittest.TestCase):\n"
            "    def test_prepare_clamps_and_offsets(self):\n"
            "        self.assertEqual(prepare(150), 101)\n\n"
            "if __name__ == '__main__':\n"
            "    unittest.main()\n"
        ),
    }

    def check(root: Path) -> bool:
        return (
            _run_unittest(root)
            and _contains_symbol(root, "math_ops.py", "clamp_value")
            and _contains_symbol(root, "pipeline.py", "clamp_value")
            and not _contains_symbol(root, "pipeline.py", "normalize_value")
        )

    return ProbeCase(
        name="python-function-rename",
        files=files,
        task=(
            "Rename normalize_value to clamp_value across this project. "
            "Update imports and callers, keep behavior, and run python -m unittest."
        ),
        check=check,
        missed=lambda root: int(_contains_symbol(root, "pipeline.py", "normalize_value")),
        wrong_extra_edits=lambda _root: 0,
    )


def _class_case() -> ProbeCase:
    files = {
        "models.py": (
            "class OrderProcessor:\n"
            "    def label(self):\n"
            "        return 'order'\n"
        ),
        "service.py": (
            "from models import OrderProcessor\n\n"
            "def build_label():\n"
            "    return OrderProcessor().label()\n"
        ),
        "test_service.py": (
            "import unittest\n"
            "from service import build_label\n\n"
            "class ServiceTests(unittest.TestCase):\n"
            "    def test_build_label(self):\n"
            "        self.assertEqual(build_label(), 'order')\n\n"
            "if __name__ == '__main__':\n"
            "    unittest.main()\n"
        ),
    }

    def check(root: Path) -> bool:
        return (
            _run_unittest(root)
            and _contains_symbol(root, "models.py", "InvoiceProcessor")
            and _contains_symbol(root, "service.py", "InvoiceProcessor")
            and not _contains_symbol(root, "service.py", "OrderProcessor")
        )

    return ProbeCase(
        name="python-class-rename",
        files=files,
        task=(
            "Rename class OrderProcessor to InvoiceProcessor across this project. "
            "Update imports and callers, keep behavior, and run python -m unittest."
        ),
        check=check,
        missed=lambda root: int(_contains_symbol(root, "service.py", "OrderProcessor")),
        wrong_extra_edits=lambda _root: 0,
    )


def _implicit_function_case() -> ProbeCase:
    files = {
        "metrics.py": (
            "def normalize_value(raw):\n"
            "    return max(0, min(100, raw))\n"
        ),
        "jobs.py": (
            "from metrics import normalize_value\n\n"
            "def score(raw):\n"
            "    return normalize_value(raw) + 5\n"
        ),
        "test_jobs.py": (
            "import unittest\n"
            "from jobs import score\n\n"
            "class JobTests(unittest.TestCase):\n"
            "    def test_score_clamps_and_offsets(self):\n"
            "        self.assertEqual(score(150), 105)\n\n"
            "if __name__ == '__main__':\n"
            "    unittest.main()\n"
        ),
    }

    def check(root: Path) -> bool:
        return (
            _run_unittest(root)
            and _contains_symbol(root, "metrics.py", "clamp_value")
            and _contains_symbol(root, "jobs.py", "clamp_value")
            and not _contains_symbol(root, "jobs.py", "normalize_value")
        )

    return ProbeCase(
        name="implicit-function-rename",
        files=files,
        task=(
            "Rename normalize_value to clamp_value. Keep behavior and run "
            "python -m unittest."
        ),
        check=check,
        missed=lambda root: int(_contains_symbol(root, "jobs.py", "normalize_value")),
        wrong_extra_edits=lambda _root: 0,
    )


def _string_control_case() -> ProbeCase:
    files = {
        "profile.py": (
            "def fetch_user_profile(user_id):\n"
            "    return {'id': user_id, 'active': True}\n"
        ),
        "router.py": (
            "from profile import fetch_user_profile\n\n"
            "ROUTE_NAME = 'fetch_user_profile'\n\n"
            "def handle(user_id):\n"
            "    return ROUTE_NAME, fetch_user_profile(user_id)\n"
        ),
        "test_router.py": (
            "import unittest\n"
            "from router import ROUTE_NAME, handle\n\n"
            "class RouterTests(unittest.TestCase):\n"
            "    def test_route_name_stays_public_contract(self):\n"
            "        self.assertEqual(ROUTE_NAME, 'fetch_user_profile')\n\n"
            "    def test_handle_uses_new_function(self):\n"
            "        route, payload = handle(7)\n"
            "        self.assertEqual(route, 'fetch_user_profile')\n"
            "        self.assertEqual(payload['id'], 7)\n\n"
            "if __name__ == '__main__':\n"
            "    unittest.main()\n"
        ),
    }

    def check(root: Path) -> bool:
        router = _text(root, "router.py")
        return (
            _run_unittest(root)
            and _contains_symbol(root, "profile.py", "load_user_profile")
            and _contains_symbol(root, "router.py", "load_user_profile")
            and "ROUTE_NAME = 'fetch_user_profile'" in router
        )

    def wrong_extra(root: Path) -> int:
        return int("ROUTE_NAME = 'fetch_user_profile'" not in _text(root, "router.py"))

    return ProbeCase(
        name="public-string-control",
        files=files,
        task=(
            "Rename the Python function fetch_user_profile to load_user_profile "
            "and update imports/calls. Keep the external ROUTE_NAME string exactly "
            "'fetch_user_profile'. Run python -m unittest."
        ),
        check=check,
        missed=lambda root: max(
            0,
            _symbol_occurrences(root, "router.py", "fetch_user_profile")
            - int("ROUTE_NAME = 'fetch_user_profile'" in _text(root, "router.py"))
        ),
        wrong_extra_edits=wrong_extra,
    )


CASES = {
    case.name: case
    for case in (
        _function_case(),
        _class_case(),
        _implicit_function_case(),
        _string_control_case(),
    )
}


def _run_arm(provider, case: ProbeCase, *, arm: str, max_turns: int) -> dict[str, Any]:
    events: list[RunEvent] = []
    with tempfile.TemporaryDirectory(prefix="codey-refactor-hint-ab-") as td:
        root = Path(td)
        _write_project(root, case.files)
        probe = _RefactorHintProbe(arm=arm)
        original_edit = agent.edit_file
        agent.edit_file = probe
        try:
            result = agent.run(
                provider,
                root,
                case.task,
                max_turns=max_turns,
                on_event=events.append,
                fresh_chat=True,
                provider_id=getattr(provider, "id", ""),
                project_map=render_project_map(root, task=case.task),
            )
        finally:
            agent.edit_file = original_edit

        final_success = case.check(root)
        missed_callers = case.missed(root)
        wrong_extra_edits = case.wrong_extra_edits(root)

    tool_events = [event for event in events if event.kind == "tool" and event.call]
    run_events = [event for event in tool_events if event.call and event.call.name == "run"]
    row = {
        "arm": arm,
        "stop_reason": result.stop_reason,
        "turns": result.turns,
        "tool_calls": len(tool_events),
        "reads": sum(1 for event in tool_events if event.call and event.call.name == "read"),
        "edits": sum(1 for event in tool_events if event.call and event.call.name == "edit"),
        "runs": len(run_events),
        "failed_runs": sum(
            1 for event in run_events if event.outcome is not None and not event.outcome.ok
        ),
        "find_references_calls": sum(
            1 for event in tool_events if event.call and event.call.name == "references"
        ),
        "generated_hints": probe.generated_hints,
        "exposed_hints": probe.exposed_hints,
        "missed_callers": missed_callers,
        "wrong_extra_edits": wrong_extra_edits,
        "final_success": final_success,
    }
    if result.stop_reason != "done" or not final_success or wrong_extra_edits:
        row["event_tail"] = [render_run_event(event) for event in events[-10:]]
    return row


def run_probe(
    provider_id: str,
    case_name: str,
    port: int,
    max_turns: int,
    arms: tuple[str, ...] = ARMS,
) -> dict[str, Any]:
    case = CASES[case_name]
    provider_controls.begin_task_context(f"refactor-hint-ab:{provider_id}:{case_name}")
    provider = None
    try:
        provider = connect_provider(provider_id, port=port)
        rows = []
        for arm in arms:
            row = _run_arm(provider, case, arm=arm, max_turns=max_turns)
            rows.append(row)
            print(
                f"[{case_name} {arm}] success={row['final_success']} "
                f"missed={row['missed_callers']} wrong_extra={row['wrong_extra_edits']} "
                f"turns={row['turns']} tools={row['tool_calls']} "
                f"failed_runs={row['failed_runs']} hints={row['exposed_hints']}",
                flush=True,
            )
    finally:
        try:
            if provider is not None:
                provider.close()
        finally:
            provider_controls.end_task_context()

    baseline = next((row for row in rows if row["arm"] == "baseline"), None)
    hint = next((row for row in rows if row["arm"] == "hint"), None)
    comparison = {}
    if baseline and hint:
        comparison = {
            "success_delta": int(hint["final_success"]) - int(baseline["final_success"]),
            "missed_callers_delta": hint["missed_callers"] - baseline["missed_callers"],
            "failed_runs_delta": hint["failed_runs"] - baseline["failed_runs"],
            "turn_delta": hint["turns"] - baseline["turns"],
            "tool_call_delta": hint["tool_calls"] - baseline["tool_calls"],
            "wrong_extra_edits_delta": hint["wrong_extra_edits"] - baseline["wrong_extra_edits"],
        }
    return {
        "provider": provider_id,
        "case": case_name,
        "arms": rows,
        "comparison": comparison,
    }


def run_self_test() -> None:
    assert _rename_pair(
        "def normalize_value(raw):\n",
        "def clamp_value(raw):\n",
    ) == ("normalize_value", "clamp_value")
    assert _rename_pair("value = 1\n", "state = 1\n") is None
    assert _rename_pair("def one(a):\n", "def two(a):\n") is None

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write_project(
            root,
            {
                "a.py": "def normalize_value(raw):\n    return raw\n",
                "b.py": "from a import normalize_value\n",
                "README.md": "normalize_value should not count in docs\n",
            },
        )
        hint = _render_refactor_hint(
            root,
            "a.py",
            [
                type(
                    "Block",
                    (),
                    {
                        "search": "def normalize_value(raw):\n",
                        "replace": "def clamp_value(raw):\n",
                    },
                )()
            ],
        )
        assert "'normalize_value' still appears in 1 other file" in hint
        assert "find_references" in hint

        no_hint = _render_refactor_hint(
            root,
            "a.py",
            [
                type(
                    "Block",
                    (),
                    {
                        "search": "value = result\n",
                        "replace": "state = result\n",
                    },
                )()
            ],
        )
        assert no_hint == ""
    print("self-test passed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Live A/B for incomplete refactor hints.")
    parser.add_argument("--provider", choices=provider_ids(), default="mimo")
    parser.add_argument("--case", choices=sorted(CASES), default="python-function-rename")
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--max-turns", type=int, default=10)
    parser.add_argument("--arm", choices=ARMS, action="append")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        run_self_test()
        return 0
    report = run_probe(
        args.provider,
        args.case,
        args.port,
        args.max_turns,
        tuple(args.arm or ARMS),
    )
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
