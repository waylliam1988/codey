"""Live A/B probe for a post-edit impact guard.

This experiment does not change production behavior. The guard arm wraps the
edit tool, infers a small number of changed definitions from replacement
blocks, performs a bounded read-only lexical reference scan, and appends a short
path:line impact note to the edit result. The note is intentionally not a
coverage proof.
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
from typing import Any, Callable, Iterable

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from codey import agent, provider_controls
from codey.agent_tools import AgentToolFns
from codey.bounded_scan import BoundedScanBudget, iter_bounded_files
from codey.events import RunEvent, render_run_event
from codey.project_map import render_project_map
from codey.providers.registry import connect_provider, provider_ids
from codey.references import REFERENCE_EXCLUDED_DIRS, find_reference_hints
from codey.tool_runtime import EditBlock, ToolOutcome
from codey.tool_runtime import edit_file as runtime_edit_file


ARMS = ("current", "impact_guard")
DEFAULT_PROVIDERS = ("deepseek", "qwen")
ALL_WEB_PROVIDERS = ("deepseek", "mimo", "qwen", "glm")
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
MAX_CHANGED_SYMBOLS = 3
MAX_REFS_PER_SYMBOL = 4
MAX_TOTAL_REFS = 8
MAX_SCAN_FILES = 800
MAX_SCAN_DIRS = 200
MAX_SCAN_DIR_ENTRIES = 800
MAX_SCAN_BYTES = 2 * 1024 * 1024

IDENTIFIER_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
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
REF_ROW_RE = re.compile(r"^- (?P<kind>[A-Za-z_]+) (?P<path>.+?):(?P<line>\d+):")


@dataclass(frozen=True)
class ChangedDefinition:
    name: str
    kind: str
    path: str
    old_name: str | None = None

    @property
    def lookup_name(self) -> str:
        return self.old_name or self.name

    @property
    def label(self) -> str:
        if self.old_name and self.old_name != self.name:
            return f"{self.old_name} -> {self.name}"
        return self.name


@dataclass(frozen=True)
class ImpactReference:
    symbol: str
    path: str
    line: int


@dataclass(frozen=True)
class ProbeCase:
    name: str
    files: dict[str, str]
    task: str
    check: Callable[[Path], bool]
    missed: Callable[[Path], int]
    wrong_extra_edits: Callable[[Path], int]


def _symbol_pattern(symbol: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z0-9_$]){re.escape(symbol)}(?![A-Za-z0-9_$])")


def _text(root: Path, rel: str) -> str:
    path = root / rel
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _contains_symbol(root: Path, rel: str, symbol: str) -> bool:
    return _symbol_pattern(symbol).search(_text(root, rel)) is not None


def _symbol_occurrences(root: Path, rel: str, symbol: str) -> int:
    return len(_symbol_pattern(symbol).findall(_text(root, rel)))


def _run_unittest(root: Path) -> bool:
    try:
        completed = subprocess.run(
            (sys.executable, "-B", "-m", "unittest"),
            cwd=root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _line_defs(text: str) -> list[tuple[str, str]]:
    defs: list[tuple[str, str]] = []
    for line in text.splitlines():
        for kind, regex in (
            ("function", PY_DEF_RE),
            ("class", PY_CLASS_RE),
            ("constant", PY_CONST_RE),
            ("function", JS_FUNC_RE),
            ("class", JS_CLASS_RE),
            ("constant", JS_CONST_RE),
        ):
            match = regex.search(line)
            if match:
                name = match.group(1)
                if IDENTIFIER_RE.fullmatch(name):
                    defs.append((kind, name))
                break
    return defs


def _changed_definitions_from_blocks(
    rel: str,
    blocks: Iterable[EditBlock],
    *,
    max_symbols: int = MAX_CHANGED_SYMBOLS,
) -> list[ChangedDefinition]:
    changed: list[ChangedDefinition] = []
    seen: set[tuple[str, str, str | None]] = set()
    for block in blocks:
        old_defs = _line_defs(str(block.search))
        new_defs = _line_defs(str(block.replace))
        old_by_kind = {}
        for kind, name in old_defs:
            old_by_kind.setdefault(kind, []).append(name)
        new_by_kind = {}
        for kind, name in new_defs:
            new_by_kind.setdefault(kind, []).append(name)

        for kind, old_names in old_by_kind.items():
            new_names = new_by_kind.get(kind, [])
            paired = min(len(old_names), len(new_names))
            for index in range(paired):
                old_name = old_names[index]
                new_name = new_names[index]
                if old_name == new_name and block.search == block.replace:
                    continue
                key = (rel, new_name, old_name)
                if key in seen:
                    continue
                seen.add(key)
                changed.append(
                    ChangedDefinition(
                        name=new_name,
                        old_name=old_name if old_name != new_name else None,
                        kind=kind,
                        path=rel,
                    )
                )
                if len(changed) >= max_symbols:
                    return changed
            for old_name in old_names[paired:]:
                key = (rel, old_name, old_name)
                if key in seen:
                    continue
                seen.add(key)
                changed.append(ChangedDefinition(name=old_name, old_name=old_name, kind=kind, path=rel))
                if len(changed) >= max_symbols:
                    return changed
    return changed


def _candidate_source_files(root: Path, budget: BoundedScanBudget) -> tuple[Path, ...]:
    return tuple(
        iter_bounded_files(
            root,
            excluded_dirs=REFERENCE_EXCLUDED_DIRS,
            budget=budget,
            allow_file=lambda path: path.suffix.lower() in SOURCE_SUFFIXES,
            skip_start_if_excluded=False,
        )
    )


def _external_references(
    root: Path,
    symbol: ChangedDefinition,
    excluded_rels: set[str],
) -> tuple[list[ImpactReference], bool]:
    budget = BoundedScanBudget(
        max_files=MAX_SCAN_FILES,
        max_dirs=MAX_SCAN_DIRS,
        max_dir_entries=MAX_SCAN_DIR_ENTRIES,
        max_bytes=MAX_SCAN_BYTES,
    )
    files = _candidate_source_files(root, budget)
    try:
        scan = find_reference_hints(
            root,
            root,
            symbol.lookup_name,
            files=files,
            scan_budget=budget,
            files_budgeted=True,
            max_results=MAX_REFS_PER_SYMBOL + len(excluded_rels) + 8,
        )
    except ValueError:
        return [], budget.limited

    refs: list[ImpactReference] = []
    seen: set[tuple[str, int]] = set()
    for line in scan.output.splitlines():
        match = REF_ROW_RE.match(line)
        if not match:
            continue
        path = match.group("path")
        if path in excluded_rels:
            continue
        line_no = int(match.group("line"))
        key = (path, line_no)
        if key in seen:
            continue
        seen.add(key)
        refs.append(ImpactReference(symbol=symbol.lookup_name, path=path, line=line_no))
        if len(refs) >= MAX_REFS_PER_SYMBOL:
            break
    incomplete = scan.truncated or budget.limited or bool(scan.report and scan.report.incomplete)
    return refs, incomplete


def _render_impact_guard(root: Path, rel: str, blocks: list[EditBlock], changed_rels: set[str]) -> str:
    symbols = _changed_definitions_from_blocks(rel, blocks)
    if not symbols:
        return ""

    all_refs: list[ImpactReference] = []
    incomplete = False
    excluded = set(changed_rels)
    excluded.add(rel)
    for symbol in symbols:
        refs, symbol_incomplete = _external_references(root, symbol, excluded)
        incomplete = incomplete or symbol_incomplete
        all_refs.extend(refs)
        if len(all_refs) >= MAX_TOTAL_REFS:
            all_refs = all_refs[:MAX_TOTAL_REFS]
            incomplete = True
            break

    if not all_refs and not incomplete:
        return ""

    lines = ["Impact Guard (read-only; not coverage proof):"]
    for symbol in symbols:
        lines.append(f"- {symbol.label} changed in {symbol.path}")
    if all_refs:
        lines.append("- external references found outside changed files:")
        for ref in all_refs:
            lines.append(f"  {ref.path}:{ref.line}")
    if incomplete:
        lines.append("- scan was bounded; omitted files may contain more references")
    lines.append("Before finishing, inspect or update affected callers if relevant.")
    return "\n".join(lines)


class _ImpactGuardProbe:
    def __init__(self, *, arm: str) -> None:
        self.arm = arm
        self.original = runtime_edit_file
        self.changed_rels: set[str] = set()
        self.generated_guards = 0
        self.exposed_guards = 0
        self.references_found = 0
        self.incomplete_scans = 0

    def __call__(self, root: Path, rel: str, blocks: list[EditBlock]) -> ToolOutcome:
        outcome = self.original(root, rel, blocks)
        if not (outcome.ok and outcome.changed):
            return outcome
        guard = _render_impact_guard(root, rel, blocks, self.changed_rels)
        self.changed_rels.add(rel)
        if guard:
            self.generated_guards += 1
            self.references_found += len(
                [line for line in guard.splitlines() if re.match(r"^\s+[^:]+:\d+$", line)]
            )
            if "scan was bounded" in guard:
                self.incomplete_scans += 1
        if self.arm != "impact_guard" or not guard:
            return outcome
        self.exposed_guards += 1
        return ToolOutcome(
            output=f"{outcome.output}\n{guard}",
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


def _python_function_case() -> ProbeCase:
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
        name="python-function-rename",
        files=files,
        task=(
            "Rename the normalize_value function in metrics.py to clamp_value. "
            "Keep the behavior the same."
        ),
        check=check,
        missed=lambda root: int(_contains_symbol(root, "jobs.py", "normalize_value")),
        wrong_extra_edits=lambda _root: 0,
    )


def _python_class_case() -> ProbeCase:
    files = {
        "processors.py": (
            "class OrderProcessor:\n"
            "    def label(self):\n"
            "        return 'order'\n"
        ),
        "legacy/batch.py": (
            "from processors import OrderProcessor\n\n"
            "def build_label():\n"
            "    return OrderProcessor().label()\n"
        ),
        "test_batch.py": (
            "import unittest\n"
            "from legacy.batch import build_label\n\n"
            "class BatchTests(unittest.TestCase):\n"
            "    def test_build_label(self):\n"
            "        self.assertEqual(build_label(), 'order')\n\n"
            "if __name__ == '__main__':\n"
            "    unittest.main()\n"
        ),
    }

    def check(root: Path) -> bool:
        return (
            _run_unittest(root)
            and _contains_symbol(root, "processors.py", "InvoiceProcessor")
            and _contains_symbol(root, "legacy/batch.py", "InvoiceProcessor")
            and not _contains_symbol(root, "legacy/batch.py", "OrderProcessor")
        )

    return ProbeCase(
        name="python-class-rename",
        files=files,
        task=(
            "Rename the OrderProcessor class in processors.py to InvoiceProcessor. "
            "Keep behavior unchanged."
        ),
        check=check,
        missed=lambda root: int(_contains_symbol(root, "legacy/batch.py", "OrderProcessor")),
        wrong_extra_edits=lambda _root: 0,
    )


def _ts_export_case() -> ProbeCase:
    files = {
        "src/api.ts": (
            "export function formatPrice(cents: number): string {\n"
            "  return `$${(cents / 100).toFixed(2)}`;\n"
            "}\n"
        ),
        "src/view.ts": (
            "import { formatPrice } from './api';\n\n"
            "export function renderCartTotal(cents: number): string {\n"
            "  return `Total: ${formatPrice(cents)}`;\n"
            "}\n"
        ),
        "package.json": json.dumps(
            {
                "type": "module",
                "scripts": {"typecheck": "tsc --noEmit"},
                "devDependencies": {"typescript": "^5.0.0"},
            },
            indent=2,
        )
        + "\n",
    }

    def check(root: Path) -> bool:
        return (
            _contains_symbol(root, "src/api.ts", "renderPrice")
            and _contains_symbol(root, "src/view.ts", "renderPrice")
            and not _contains_symbol(root, "src/view.ts", "formatPrice")
        )

    return ProbeCase(
        name="ts-exported-function-rename",
        files=files,
        task=(
            "Rename the exported formatPrice function in src/api.ts to renderPrice. "
            "Keep the implementation behavior the same."
        ),
        check=check,
        missed=lambda root: int(_contains_symbol(root, "src/view.ts", "formatPrice")),
        wrong_extra_edits=lambda _root: 0,
    )


def _delete_legacy_case() -> ProbeCase:
    files = {
        "discounts.py": (
            "def compute_discount(amount):\n"
            "    return amount * 0.10\n\n"
            "def calculate_discount(amount):\n"
            "    return compute_discount(amount)\n"
        ),
        "legacy/invoices.py": (
            "from discounts import calculate_discount\n\n"
            "def invoice_discount(amount):\n"
            "    return calculate_discount(amount)\n"
        ),
        "test_invoices.py": (
            "import unittest\n"
            "from legacy.invoices import invoice_discount\n\n"
            "class InvoiceTests(unittest.TestCase):\n"
            "    def test_discount(self):\n"
            "        self.assertEqual(invoice_discount(200), 20)\n\n"
            "if __name__ == '__main__':\n"
            "    unittest.main()\n"
        ),
    }

    def check(root: Path) -> bool:
        return _run_unittest(root) and (
            _contains_symbol(root, "discounts.py", "calculate_discount")
            or _contains_symbol(root, "legacy/invoices.py", "compute_discount")
        )

    return ProbeCase(
        name="delete-legacy-reference",
        files=files,
        task=(
            "Remove calculate_discount from discounts.py if it is unused, because "
            "compute_discount is now the preferred helper."
        ),
        check=check,
        missed=lambda root: int(
            not _contains_symbol(root, "discounts.py", "calculate_discount")
            and _contains_symbol(root, "legacy/invoices.py", "calculate_discount")
        ),
        wrong_extra_edits=lambda _root: 0,
    )


def _public_string_control_case() -> ProbeCase:
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
            "'fetch_user_profile'."
        ),
        check=check,
        missed=lambda root: max(
            0,
            _symbol_occurrences(root, "router.py", "fetch_user_profile")
            - int("ROUTE_NAME = 'fetch_user_profile'" in _text(root, "router.py")),
        ),
        wrong_extra_edits=wrong_extra,
    )


CASES = {
    case.name: case
    for case in (
        _python_function_case(),
        _python_class_case(),
        _ts_export_case(),
        _delete_legacy_case(),
        _public_string_control_case(),
    )
}


def _run_arm(provider, case: ProbeCase, *, arm: str, max_turns: int) -> dict[str, Any]:
    events: list[RunEvent] = []
    with tempfile.TemporaryDirectory(prefix="codey-impact-guard-ab-") as td:
        root = Path(td)
        _write_project(root, case.files)
        probe = _ImpactGuardProbe(arm=arm)
        result = agent.run(
            provider,
            root,
            case.task,
            max_turns=max_turns,
            on_event=events.append,
            fresh_chat=True,
            provider_id=getattr(provider, "id", ""),
            project_map=render_project_map(root, task=case.task),
            tool_fns=AgentToolFns(edit_file=probe),
        )
        final_success = case.check(root)
        missed_callers = case.missed(root)
        wrong_extra_edits = case.wrong_extra_edits(root)

    tool_events = [event for event in events if event.kind == "tool" and event.call]
    run_events = [event for event in tool_events if event.call and event.call.name == "run"]
    row = {
        "case": case.name,
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
        "references_tool_calls": sum(
            1 for event in tool_events if event.call and event.call.name == "references"
        ),
        "generated_guards": probe.generated_guards,
        "exposed_guards": probe.exposed_guards,
        "references_found_by_guard": probe.references_found,
        "incomplete_scans": probe.incomplete_scans,
        "missed_callers": missed_callers,
        "wrong_extra_edits": wrong_extra_edits,
        "final_success": final_success,
    }
    if result.stop_reason != "done" or not final_success or wrong_extra_edits:
        row["event_tail"] = [render_run_event(event) for event in events[-12:]]
    return row


def _comparison(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_case: dict[str, dict[str, dict[str, Any]]] = {}
    for row in rows:
        if "error" in row:
            continue
        by_case.setdefault(str(row["case"]), {})[str(row["arm"])] = row
    comparisons = []
    for case, arms in sorted(by_case.items()):
        current = arms.get("current")
        guard = arms.get("impact_guard")
        if not current or not guard:
            continue
        guard_exposed = bool(guard.get("exposed_guards"))
        success_delta = int(guard["final_success"]) - int(current["final_success"])
        missed_delta = guard["missed_callers"] - current["missed_callers"]
        wrong_extra_delta = guard["wrong_extra_edits"] - current["wrong_extra_edits"]
        comparisons.append(
            {
                "case": case,
                "guard_exposed": guard_exposed,
                "success_delta": success_delta,
                "missed_callers_delta": missed_delta,
                "wrong_extra_edits_delta": wrong_extra_delta,
                "turn_delta": guard["turns"] - current["turns"],
                "tool_call_delta": guard["tool_calls"] - current["tool_calls"],
                "failed_runs_delta": guard["failed_runs"] - current["failed_runs"],
                "exposed_regression": guard_exposed
                and (success_delta < 0 or missed_delta > 0 or wrong_extra_delta > 0),
            }
        )
    wins = sum(
        1
        for item in comparisons
        if item["success_delta"] > 0 or item["missed_callers_delta"] < 0
    )
    regressions = sum(
        1
        for item in comparisons
        if item["exposed_regression"]
    )
    arm_regressions = sum(
        1
        for item in comparisons
        if item["success_delta"] < 0
        or item["wrong_extra_edits_delta"] > 0
        or item["missed_callers_delta"] > 0
    )
    return {
        "cases": comparisons,
        "wins": wins,
        "exposed_regressions": regressions,
        "arm_regressions": arm_regressions,
    }


def run_probe(
    provider_ids_to_run: tuple[str, ...],
    cases: tuple[ProbeCase, ...],
    arms: tuple[str, ...],
    *,
    port: int,
    max_turns: int,
) -> dict[str, Any]:
    provider_reports = []
    for provider_id in provider_ids_to_run:
        provider_controls.begin_task_context(f"impact-guard-ab:{provider_id}")
        provider = None
        rows = []
        try:
            provider = connect_provider(provider_id, port=port)
            for case in cases:
                for arm in arms:
                    try:
                        row = _run_arm(provider, case, arm=arm, max_turns=max_turns)
                    except Exception as exc:
                        row = {
                            "provider": provider_id,
                            "case": case.name,
                            "arm": arm,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    rows.append(row)
                    if "error" in row:
                        print(f"[{provider_id} {case.name} {arm}] {row['error']}", flush=True)
                    else:
                        print(
                            f"[{provider_id} {case.name} {arm}] "
                            f"success={row['final_success']} missed={row['missed_callers']} "
                            f"wrong_extra={row['wrong_extra_edits']} turns={row['turns']} "
                            f"tools={row['tool_calls']} guards={row['exposed_guards']} "
                            f"refs={row['references_found_by_guard']}",
                            flush=True,
                        )
        finally:
            try:
                if provider is not None:
                    provider.close()
            finally:
                provider_controls.end_task_context()
        provider_reports.append(
            {
                "provider": provider_id,
                "results": rows,
                "comparison": _comparison(rows),
            }
        )
    return {"providers": provider_reports}


def run_self_test() -> None:
    blocks = [
        EditBlock(
            "def normalize_value(raw):\n    return raw\n",
            "def clamp_value(raw):\n    return raw\n",
        )
    ]
    symbols = _changed_definitions_from_blocks("metrics.py", blocks)
    assert symbols == [
        ChangedDefinition(
            name="clamp_value",
            old_name="normalize_value",
            kind="function",
            path="metrics.py",
        )
    ]
    assert _changed_definitions_from_blocks(
        "app.ts",
        [EditBlock("export function formatPrice(cents: number) {\n", "export function renderPrice(cents: number) {\n")],
    )[0].lookup_name == "formatPrice"
    assert len(
        _changed_definitions_from_blocks(
            "many.py",
            [
                EditBlock(
                    "def first():\nclass Second:\nTHIRD_VALUE = 1\nFOURTH_VALUE = 2\n",
                    "def first_new():\nclass SecondNew:\nTHIRD_NEW = 1\nFOURTH_NEW = 2\n",
                )
            ],
        )
    ) == MAX_CHANGED_SYMBOLS

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write_project(
            root,
            {
                "metrics.py": "def clamp_value(raw):\n    return raw\n",
                "jobs.py": "from metrics import normalize_value\n\nscore = normalize_value(3)\n",
                "README.md": "normalize_value in docs should not be scanned by this probe\n",
            },
        )
        guard = _render_impact_guard(root, "metrics.py", blocks, set())
        assert "Impact Guard (read-only; not coverage proof):" in guard
        assert "- normalize_value -> clamp_value changed in metrics.py" in guard
        assert "jobs.py:1" in guard
        assert "from metrics import normalize_value" not in guard
        assert "score = normalize_value" not in guard
    print("self-test passed")


def _selected_cases(names: list[str] | None) -> tuple[ProbeCase, ...]:
    if not names:
        return (CASES["python-function-rename"],)
    return tuple(CASES[name] for name in names)


def _selected_providers(values: list[str] | None, *, all_providers: bool) -> tuple[str, ...]:
    if all_providers:
        return ALL_WEB_PROVIDERS
    return tuple(values or DEFAULT_PROVIDERS)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Live A/B for post-edit impact guard.")
    parser.add_argument("--provider", choices=provider_ids(), action="append")
    parser.add_argument("--all-providers", action="store_true")
    parser.add_argument("--case", choices=sorted(CASES), action="append")
    parser.add_argument("--arm", choices=ARMS, action="append")
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--max-turns", type=int, default=10)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        run_self_test()
        return 0

    providers = _selected_providers(args.provider, all_providers=args.all_providers)
    cases = _selected_cases(args.case)
    arms = tuple(args.arm or ARMS)
    report = run_probe(providers, cases, arms, port=args.port, max_turns=args.max_turns)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
