"""Manual A/B for whether a scoped task plan improves navigation.

This probe does not change production behavior. It asks a live web provider to
choose the first files it would inspect for broad project tasks, then compares:

* current: current task-aware Project Map only
* hint: current Project Map plus a deterministic local scope hint
* scoped: a hidden, temporary Scoped Task Plan followed by file selection

The probe is deliberately read-only: it never calls Agent edit/run tools and it
writes JSON reports outside the tested repositories by default.
"""

# ruff: noqa: E402 - direct script execution must add the repository root first.

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codey.providers import controls as provider_controls
from codey.workspace.bounded_scan import BoundedScanBudget, iter_bounded_files
from codey.workspace.map import (
    EXCLUDED_DIRS,
    SOURCE_SUFFIXES,
    _iter_symbol_source_files,
    _path_blocked,
    _safe_relative,
    _symbol_score,
    _symbols_for_file,
)
from codey.providers.registry import DEFAULT_PROVIDER_ID, connect_provider, provider_ids
from codey.toolchain.runtime import list_directory
from tests.manual.project_task_context import render_production_project_map

DEFAULT_OUTPUT = Path(tempfile.gettempdir()) / "codey-scoped-task-plan-ab.json"
ARMS = ("current", "hint", "scoped")
MAX_RAW_REPLY_CHARS = 3_000
MAX_HINT_FILES = 8
MAX_HINT_DIRS = 5
MAX_HINT_SYMBOLS = 3
MAX_HINT_CHARS = 1_800

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
except (AttributeError, OSError):
    pass


@dataclass(frozen=True)
class ProbeCase:
    name: str
    project: Path
    task: str
    expected_paths: tuple[str, ...]
    expected_tests: tuple[str, ...] = ()
    expected_terms: tuple[str, ...] = ()
    tags: tuple[str, ...] = field(default_factory=tuple)


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


def cases(stockalarm: Path | None = None) -> dict[str, ProbeCase]:
    values: list[ProbeCase] = [
        ProbeCase(
            name="scoped-navigation-brief",
            project=ROOT,
            task=(
                "Add a hidden run-scoped Scoped Navigation Brief before the Writer "
                "starts. It should reuse the existing advisory/private context "
                "style, avoid persistence and UI, and help large project tasks "
                "choose a smaller first workset. Which files should be inspected "
                "first?"
            ),
            expected_paths=(
                "codey/app/task_runner.py",
                "codey/workspace/change_brief.py",
                "codey/agents/consensus.py",
            ),
            expected_tests=(
                "tests/test_server.py",
                "tests/test_change_brief.py",
            ),
            expected_terms=("ChangeBrief", "run_project_audit", "Project Map"),
            tags=("codey", "broad"),
        ),
        ProbeCase(
            name="manual-probe-harness",
            project=ROOT,
            task=(
                "Create a manual read-only A/B benchmark to test whether scoped "
                "planning improves first-file selection on large project tasks. "
                "It should live with the existing manual probes and record JSON "
                "metrics. Which files should be inspected first?"
            ),
            expected_paths=(
                "tests/manual/large_project_ab.py",
                "tests/manual/project_map_symbol_ab.py",
                "tests/manual/README.md",
            ),
            expected_tests=("tests/test_large_project_ab.py",),
            expected_terms=("read-only", "Project Map", "provider"),
            tags=("codey", "probe"),
        ),
        ProbeCase(
            name="advisory-review-context",
            project=ROOT,
            task=(
                "Tighten post-change review so private task briefs, Project Map, "
                "and verification candidates are treated only as advisory context "
                "and never as proof or changed-file paths. Which files should be "
                "inspected first?"
            ),
            expected_paths=("codey/reviews/core.py", "codey/workspace/change_brief.py"),
            expected_tests=("tests/test_review.py", "tests/test_change_brief.py"),
            expected_terms=("Verification Map", "findings", "Changed files"),
            tags=("codey", "review"),
        ),
        ProbeCase(
            name="writer-takeover-checkpoint",
            project=ROOT,
            task=(
                "Debug provider takeover after a Writer fails mid-task. Ensure "
                "the next Writer receives refreshed local checkpoint facts, shares "
                "the turn budget, and does not persist model plans. Which files "
                "should be inspected first?"
            ),
            expected_paths=(
                "codey/agents/writer_failover.py",
                "codey/app/task_runner.py",
                "codey/runs/work_checkpoint.py",
            ),
            expected_tests=(
                "tests/test_writer_failover.py",
                "tests/test_work_checkpoint_flow.py",
            ),
            expected_terms=("CheckpointView", "WriterFailoverRunner"),
            tags=("codey", "failover"),
        ),
        ProbeCase(
            name="json-tool-batching",
            project=ROOT,
            task=(
                "Change how the JSON protocol validates read_files and parallel "
                "batching, including invalid nested calls and max batch sizes. "
                "Which files should be inspected first?"
            ),
            expected_paths=("codey/protocols/json_codec.py",),
            expected_tests=("tests/test_protocols.py",),
            expected_terms=("parallel", "read_files", "JsonToolCodec"),
            tags=("codey", "protocol"),
        ),
        ProbeCase(
            name="monorepo-verification-selection",
            project=ROOT,
            task=(
                "Improve default verification command selection for monorepos: "
                "respect the closest cwd, avoid ambiguous candidates, and skip "
                "documentation-only changes. Which files should be inspected "
                "first?"
            ),
            expected_paths=(
                "codey/verification_policy.py",
                "codey/verification_map.py",
            ),
            expected_tests=(
                "tests/test_verification_policy.py",
                "tests/test_verification_map.py",
            ),
            expected_terms=("select_verification_candidate", "document"),
            tags=("codey", "verification"),
        ),
    ]

    stockalarm_root = stockalarm.expanduser().resolve() if stockalarm else None
    if stockalarm_root is not None and stockalarm_root.exists():
        values.extend([
            ProbeCase(
                name="stockalarm-training-flow",
                project=stockalarm_root,
                task=(
                    "Read-only navigation: explain how master_trainer.py main "
                    "chooses, skips, and starts training stages, and how it connects "
                    "to feature_engineering.py. Which files should be inspected first?"
                ),
                expected_paths=("master_trainer.py", "feature_engineering.py"),
                expected_terms=("main", "start_from", "feature_engineering"),
                tags=("external", "stockalarm"),
            ),
            ProbeCase(
                name="stockalarm-backtest-masks",
                project=stockalarm_root,
                task=(
                    "Read-only navigation: find the backtest entry and exit tradable "
                    "mask builders in stockalarm.py, their main callers, and their "
                    "responsibilities. Which files should be inspected first?"
                ),
                expected_paths=("stockalarm.py",),
                expected_terms=(
                    "_build_stockalarm_backtest_entry_tradable_mask",
                    "_build_stockalarm_backtest_exit_tradable_mask",
                ),
                tags=("external", "stockalarm"),
            ),
        ])
    return {case.name: case for case in values}


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


def _string_list(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return ()
    result: list[str] = []
    for item in value:
        text = str(item or "").replace("\\", "/").strip()
        text = text.removeprefix("./").strip("/")
        if text and text not in result:
            result.append(text)
    return tuple(result)


def _paths_from_reply(text: str) -> tuple[str, ...]:
    obj = _extract_json_object(text)
    paths = list(_string_list(obj.get("paths")))
    for key in ("files", "target_files", "implementation_paths"):
        for path in _string_list(obj.get(key)):
            if path not in paths:
                paths.append(path)
    return tuple(paths)


def _test_paths_from_reply(text: str) -> tuple[str, ...]:
    obj = _extract_json_object(text)
    tests = list(_string_list(obj.get("test_paths")))
    for key in ("tests", "test_files"):
        for path in _string_list(obj.get(key)):
            if path not in tests:
                tests.append(path)
    return tuple(tests)


def _plan_paths(plan: dict[str, Any]) -> tuple[str, ...]:
    paths: list[str] = []
    scopes = plan.get("scopes")
    if not isinstance(scopes, list):
        return ()
    for scope in scopes:
        if not isinstance(scope, dict):
            continue
        for key in ("paths", "files", "target_files", "test_paths", "tests"):
            for path in _string_list(scope.get(key)):
                if path not in paths:
                    paths.append(path)
    return tuple(paths)


def _norm_path(value: str) -> str:
    text = str(value or "").replace("\\", "/").strip().strip("/")
    text = text.removeprefix("./")
    parts = PurePosixPath(text).parts
    if not parts or PurePosixPath(text).is_absolute() or ".." in parts:
        return ""
    return PurePosixPath(*parts).as_posix().lower()


def _path_matches(candidate: str, expected: str) -> bool:
    cand = _norm_path(candidate)
    exp = _norm_path(expected)
    if not cand or not exp:
        return False
    return cand == exp or cand.endswith("/" + exp) or exp.endswith("/" + cand)


def _score_paths(paths: tuple[str, ...], expected: tuple[str, ...]) -> dict[str, Any]:
    hits = []
    for item in expected:
        if any(_path_matches(path, item) for path in paths):
            hits.append(item)
    first_expected_rank = None
    for index, path in enumerate(paths, start=1):
        if any(_path_matches(path, item) for item in expected):
            first_expected_rank = index
            break
    return {
        "hits": hits,
        "hit_count": len(hits),
        "expected_total": len(expected),
        "first_expected_rank": first_expected_rank,
        "top1_hit": bool(paths and any(_path_matches(paths[0], item) for item in expected)),
    }


def _score_terms(text: str, expected_terms: tuple[str, ...]) -> dict[str, Any]:
    lower = (text or "").lower()
    hits = [term for term in expected_terms if term.lower() in lower]
    return {
        "hits": hits,
        "hit_count": len(hits),
        "expected_total": len(expected_terms),
    }


def _project_context(root: Path, task: str) -> str:
    return "\n\n".join([
        "Initial listing:",
        list_directory(root, ".").model_text,
        render_production_project_map(root, task=task),
    ])


def _deterministic_scope_hint(root: Path, task: str) -> str:
    root = root.expanduser().resolve()
    candidates: dict[str, tuple[int, tuple[str, ...]]] = {}
    for path in _iter_symbol_source_files(root):
        rel = _safe_relative(root, path)
        if not rel:
            continue
        symbols = _symbols_for_file(path)
        score = _symbol_score(rel, symbols, task)
        if score <= 0:
            continue
        candidates[rel] = (score, symbols)

    budget = BoundedScanBudget()
    for path in iter_bounded_files(
        root,
        excluded_dirs=EXCLUDED_DIRS,
        budget=budget,
        allow_dir=lambda item: not _path_blocked(_safe_relative(root, item)),
        allow_file=lambda item: (
            item.suffix.lower() in SOURCE_SUFFIXES
            and not _path_blocked(_safe_relative(root, item))
        ),
        skip_start_if_excluded=False,
    ):
        rel = _safe_relative(root, path)
        if not rel:
            continue
        score = _symbol_score(rel, (), task)
        if score <= 0:
            continue
        prior = candidates.get(rel)
        if prior is None or score > prior[0]:
            candidates[rel] = (score, ())

    scored = [
        (score, rel, symbols)
        for rel, (score, symbols) in candidates.items()
    ]
    scored.sort(key=lambda item: (-item[0], item[1]))

    dir_scores: dict[str, int] = {}
    for score, rel, _symbols in scored:
        parent = PurePosixPath(rel).parent.as_posix()
        label = "." if parent == "." else parent + "/"
        dir_scores[label] = max(score, dir_scores.get(label, 0))
    dirs = sorted(dir_scores.items(), key=lambda item: (-item[1], item[0]))[:MAX_HINT_DIRS]

    lines = [
        "Deterministic Scope Hint (local, advisory; no model planning):",
        "- Use this only to choose the first files to inspect; it is not impact proof.",
    ]
    if dirs:
        lines.append("Candidate dirs:")
        for rel, score in dirs:
            lines.append(f"- {rel} (score {score})")
    if scored:
        lines.append("Candidate files:")
        for score, rel, symbols in scored[:MAX_HINT_FILES]:
            suffix = ""
            if symbols:
                suffix = ": " + "; ".join(symbols[:MAX_HINT_SYMBOLS])
            lines.append(f"- {rel} (score {score}){suffix}")
    if len(lines) == 2:
        lines.append("- no positive local hint candidates")
    rendered = "\n".join(lines)
    if len(rendered) > MAX_HINT_CHARS:
        rendered = rendered[:MAX_HINT_CHARS].rstrip() + "\n- scope hint truncated"
    return rendered


def _current_prompt(case: ProbeCase) -> str:
    return "\n".join([
        "You are evaluating a local coding agent's project navigation.",
        "Do not solve or implement the task. Choose the first files to inspect.",
        "Use only relative paths. Prefer existing implementation files and focused tests.",
        "Return exactly one JSON object, no markdown:",
        (
            '{"paths":["relative/implementation.py"],'
            '"test_paths":["relative/test_file.py"],"reason":"short reason"}'
        ),
        "",
        _project_context(case.project, case.task),
        "",
        "Task:",
        case.task,
    ])


def _hint_prompt(case: ProbeCase) -> str:
    return "\n".join([
        "You are evaluating a local coding agent's project navigation.",
        "Do not solve or implement the task. Choose the first files to inspect.",
        "Use only relative paths. Prefer existing implementation files and focused tests.",
        "The deterministic scope hint is local and advisory, not proof.",
        "Return exactly one JSON object, no markdown:",
        (
            '{"paths":["relative/implementation.py"],'
            '"test_paths":["relative/test_file.py"],"reason":"short reason"}'
        ),
        "",
        _project_context(case.project, case.task),
        "",
        _deterministic_scope_hint(case.project, case.task),
        "",
        "Task:",
        case.task,
    ])


def _scoped_plan_prompt(case: ProbeCase) -> str:
    return "\n".join([
        "You are a hidden navigation planner for a local coding agent.",
        "Do not solve or implement the task. Do not write code.",
        "Create a temporary Scoped Task Plan whose only job is to shrink the first workset.",
        "The plan is advisory, run-scoped, not persisted, not UI, and not impact proof.",
        "Return exactly one JSON object, no markdown:",
        (
            '{"scopes":[{"name":"scope name","dirs":["relative/dir"],'
            '"paths":["relative/file.py"],"test_paths":["relative/test_file.py"],'
            '"queries":["literal search"],"checks":["candidate command"],'
            '"reason":"why this scope is first"}],'
            '"integration":{"checks":["candidate command"],"risks":["risk"]}}'
        ),
        "Rules:",
        "- Use 2 to 4 scopes.",
        "- Keep each scope small enough for one bounded Writer pass.",
        "- Planner narrows navigation only; Writer must verify by reading files.",
        "- Do not invent persistence, indexes, LSP, RAG, or new UI.",
        "",
        _project_context(case.project, case.task),
        "",
        "Task:",
        case.task,
    ])


def _scoped_selection_prompt(case: ProbeCase, plan_reply: str) -> str:
    plan = _extract_json_object(plan_reply)
    plan_json = json.dumps(plan, ensure_ascii=False, indent=2) if plan else _clip(plan_reply)
    return "\n".join([
        "Now choose only the first files the Writer should inspect.",
        "Use the temporary Scoped Task Plan as advisory navigation context.",
        "Do not solve or implement the task. Return exactly one JSON object, no markdown:",
        (
            '{"chosen_scope":"scope name","paths":["relative/implementation.py"],'
            '"test_paths":["relative/test_file.py"],"reason":"short reason"}'
        ),
        "",
        "Scoped Task Plan:",
        plan_json,
        "",
        _project_context(case.project, case.task),
        "",
        "Task:",
        case.task,
    ])


def _score_result(
    *,
    case: ProbeCase,
    paths: tuple[str, ...],
    test_paths: tuple[str, ...],
    text: str,
) -> dict[str, Any]:
    return {
        "paths": _score_paths(paths, case.expected_paths),
        "tests": _score_paths(test_paths, case.expected_tests),
        "terms": _score_terms(text, case.expected_terms),
    }


def run_arm(
    provider: CountingProvider,
    case: ProbeCase,
    arm: str,
    *,
    timeout: float,
) -> dict[str, Any]:
    provider.new_chat()
    sends_before = provider.sends
    sent_before = provider.sent_chars
    reply_before = provider.reply_chars
    seconds_before = provider.seconds
    started = time.monotonic()

    if arm == "current":
        prompt = _current_prompt(case)
        reply = provider.send(prompt, timeout=timeout)
        paths = _paths_from_reply(reply)
        test_paths = _test_paths_from_reply(reply)
        combined_text = reply
        plan = {}
        plan_reply = ""
    elif arm == "hint":
        prompt = _hint_prompt(case)
        reply = provider.send(prompt, timeout=timeout)
        paths = _paths_from_reply(reply)
        test_paths = _test_paths_from_reply(reply)
        combined_text = reply
        plan = {}
        plan_reply = ""
    elif arm == "scoped":
        plan_prompt = _scoped_plan_prompt(case)
        plan_reply = provider.send(plan_prompt, timeout=timeout)
        plan = _extract_json_object(plan_reply)
        selection_prompt = _scoped_selection_prompt(case, plan_reply)
        reply = provider.send(selection_prompt, timeout=timeout)
        paths = _paths_from_reply(reply)
        test_paths = _test_paths_from_reply(reply)
        combined_text = f"{plan_reply}\n{reply}"
        if not paths:
            paths = _plan_paths(plan)
    else:
        raise ValueError(f"unknown arm: {arm}")

    score = _score_result(
        case=case,
        paths=paths,
        test_paths=test_paths,
        text=combined_text,
    )
    return {
        "case": case.name,
        "arm": arm,
        "project": case.project.as_posix(),
        "tags": list(case.tags),
        "task": case.task,
        "expected_paths": list(case.expected_paths),
        "expected_tests": list(case.expected_tests),
        "expected_terms": list(case.expected_terms),
        "paths": list(paths),
        "test_paths": list(test_paths),
        "score": score,
        "ok": (
            score["paths"]["hit_count"] > 0
            or score["tests"]["hit_count"] > 0
            or score["terms"]["hit_count"] > 0
        ),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "provider_seconds": round(provider.seconds - seconds_before, 3),
        "sends": provider.sends - sends_before,
        "sent_chars": provider.sent_chars - sent_before,
        "reply_chars": provider.reply_chars - reply_before,
        "plan_scope_count": len(plan.get("scopes") or []) if isinstance(plan, dict) else 0,
        "plan_paths": list(_plan_paths(plan)),
        "raw_plan_reply": _clip(plan_reply),
        "raw_selection_reply": _clip(reply),
    }


def _arm_order(order: str) -> tuple[str, ...]:
    if order == "scoped-first":
        return ("scoped", "current", "hint")
    if order == "hint-first":
        return ("hint", "current", "scoped")
    if order == "current-first":
        return ("current", "hint", "scoped")
    raise ValueError(f"unknown order: {order}")


def _summarize_rows(
    rows: list[dict[str, Any]],
    *,
    arms: tuple[str, ...] = ARMS,
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "completed_rows": len([row for row in rows if "error" not in row]),
        "errors": len([row for row in rows if "error" in row]),
        "by_arm": {},
        "deltas_vs_current": {},
    }
    for arm in arms:
        arm_rows = [row for row in rows if row.get("arm") == arm and "error" not in row]
        summary["by_arm"][arm] = {
            "rows": len(arm_rows),
            "ok": sum(1 for row in arm_rows if row.get("ok")),
            "path_hits": sum(row["score"]["paths"]["hit_count"] for row in arm_rows),
            "test_hits": sum(row["score"]["tests"]["hit_count"] for row in arm_rows),
            "term_hits": sum(row["score"]["terms"]["hit_count"] for row in arm_rows),
            "top1_path_hits": sum(
                1 for row in arm_rows if row["score"]["paths"]["top1_hit"]
            ),
            "sent_chars": sum(int(row.get("sent_chars") or 0) for row in arm_rows),
            "provider_seconds": round(
                sum(float(row.get("provider_seconds") or 0.0) for row in arm_rows),
                3,
            ),
        }
    current = summary["by_arm"].get("current")
    if current is not None:
        for arm in arms:
            if arm == "current":
                continue
            other = summary["by_arm"].get(arm)
            if other is None:
                continue
            delta: dict[str, Any] = {}
            for key in ("ok", "path_hits", "test_hits", "term_hits", "top1_path_hits"):
                delta[key] = other[key] - current[key]
            delta["sent_chars"] = other["sent_chars"] - current["sent_chars"]
            delta["provider_seconds"] = round(
                other["provider_seconds"] - current["provider_seconds"],
                3,
            )
            summary["deltas_vs_current"][arm] = delta
    return summary


def run_provider(
    provider_id: str,
    selected_cases: tuple[ProbeCase, ...],
    *,
    port: int,
    timeout: float,
    order: str,
    arms: tuple[str, ...],
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    raw = None
    provider_controls.begin_task_context(f"scoped-task-plan-ab:{provider_id}")
    try:
        raw = connect_provider(provider_id, port=port)
        provider = CountingProvider(raw)
        for case in selected_cases:
            ordered_arms = _arm_order(order)
            ordered = tuple(arm for arm in ordered_arms if arm in arms)
            ordered += tuple(arm for arm in arms if arm not in ordered)
            for arm in ordered:
                try:
                    row = run_arm(provider, case, arm, timeout=timeout)
                    row["provider"] = provider_id
                except Exception as exc:
                    row = {
                        "provider": provider_id,
                        "case": case.name,
                        "arm": arm,
                        "error": str(exc),
                    }
                rows.append(row)
                status = "ERR" if "error" in row else (
                    f"paths={row['score']['paths']['hit_count']} "
                    f"tests={row['score']['tests']['hit_count']} "
                    f"terms={row['score']['terms']['hit_count']}"
                )
                print(f"[{provider_id}] {case.name} {arm}: {status}", flush=True)
    finally:
        try:
            if raw is not None:
                raw.close()
        finally:
            provider_controls.end_task_context()
    return {
        "provider": provider_id,
        "rows": rows,
        "summary": _summarize_rows(rows, arms=arms),
    }


def run_self_test() -> None:
    text = 'prefix {"paths":["./codey/app/task_runner.py"],"tests":["tests/test_server.py"]}'
    assert _paths_from_reply(text) == ("codey/app/task_runner.py",)
    assert _test_paths_from_reply(text) == ("tests/test_server.py",)
    score = _score_paths(("task_runner.py",), ("codey/app/task_runner.py",))
    assert score["hit_count"] == 1, score
    term_score = _score_terms("Private ChangeBrief and Project Map", ("ChangeBrief",))
    assert term_score["hit_count"] == 1, term_score
    case = cases()["scoped-navigation-brief"]
    scoped_prompt = _scoped_plan_prompt(case)
    assert "not persisted" in scoped_prompt
    assert "Do not invent persistence" in scoped_prompt
    current_prompt = _current_prompt(case)
    assert "Project Map" in current_prompt
    hint = _deterministic_scope_hint(ROOT, "debug provider takeover checkpoint")
    assert "Deterministic Scope Hint" in hint
    print("self-test passed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Compare current Project Map navigation with deterministic hints "
            "and/or a temporary Scoped Task Plan."
        )
    )
    parser.add_argument("--provider", choices=(*provider_ids(), "all"), default=DEFAULT_PROVIDER_ID)
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--case", action="append")
    parser.add_argument("--max-cases", type=int, default=0)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument(
        "--arms",
        default="current,hint",
        help="Comma-separated arms to run: current,hint,scoped.",
    )
    parser.add_argument(
        "--order",
        choices=("current-first", "hint-first", "scoped-first"),
        default="current-first",
    )
    parser.add_argument("--stockalarm", type=Path, default=Path("E:/stockalarm/stockalarm"))
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)

    available = cases(args.stockalarm)
    if args.self_test:
        run_self_test()
        return 0
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
    selected_arms = tuple(
        arm.strip()
        for arm in str(args.arms or "").split(",")
        if arm.strip()
    )
    bad_arms = [arm for arm in selected_arms if arm not in ARMS]
    if bad_arms:
        parser.error(f"unknown arm(s): {', '.join(bad_arms)}")
    selected_providers = provider_ids() if args.provider == "all" else (args.provider,)
    reports = [
        run_provider(
            provider_id,
            selected,
            port=args.port,
            timeout=args.timeout,
            order=args.order,
            arms=selected_arms,
        )
        for provider_id in selected_providers
    ]
    rows = [row for report in reports for row in report["rows"]]
    report = {
        "probe": "scoped_task_plan_ab",
        "providers": list(selected_providers),
        "arms": list(selected_arms),
        "case_count": len(selected),
        "cases": [case.name for case in selected],
        "provider_reports": reports,
        "summary": _summarize_rows(rows, arms=selected_arms),
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if report["summary"]["completed_rows"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
