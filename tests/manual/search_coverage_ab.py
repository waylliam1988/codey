"""Live A/B for search coverage hints.

The baseline arm reconstructs the pre-0.1.47 Writer search behavior for files that
cannot be decoded or read: those files are silently skipped. The coverage arm
keeps the ordinary search output, but appends a short Scan coverage note for
silent omissions and marks the result truncated so the JSON protocol warns the
model not to treat omitted content as clean.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from codey import agent, provider_controls
from codey.bounded_scan import BoundedScanBudget, iter_bounded_files
from codey.events import RunEvent, render_run_event
from codey.providers.registry import connect_provider, provider_ids
from codey.scan_report import ScanReport
from codey.tool_runtime import (
    SEARCH_EXCLUDED_DIRS,
    SEARCH_MAX_DIR_ENTRIES,
    SEARCH_MAX_FILE_BYTES,
    SEARCH_MAX_RESULTS,
    SEARCH_MAX_SCAN_BYTES,
    SEARCH_MAX_SCAN_DIRS,
    SEARCH_MAX_SCAN_FILES,
    ToolOutcome,
    _byte_limit_label,
    _raw_path_symlink_reason,
    safe_join,
)


ARMS = ("baseline", "coverage")
DEFAULT_OUTPUT = Path(tempfile.gettempdir()) / "codey-search-coverage-ab.json"
NON_UTF8_MARKER = "RARE_NON_UTF8_SEARCH_MARKER"
UNREADABLE_MARKER = "RARE_UNREADABLE_SEARCH_MARKER"
OVERSIZED_MARKER = "RARE_OVERSIZED_SEARCH_MARKER"
BUDGET_MARKER = "RARE_BUDGET_SEARCH_MARKER"


@dataclass(frozen=True)
class ProbeCase:
    name: str
    query: str
    task: str
    unreadable_path: str = ""
    max_file_bytes: int | None = None
    max_scan_files: int | None = None


CASES = {
    "search-non-utf8-omission": ProbeCase(
        name="search-non-utf8-omission",
        query=NON_UTF8_MARKER,
        task=(
            "Read-only coverage probe. Determine whether "
            f"{NON_UTF8_MARKER} is definitely absent from the project. Use grep "
            "first. If the search reports skipped, omitted, non-UTF-8, unreadable, "
            "truncated, or incomplete files, do not claim definite absence; say "
            "the search is incomplete and name the omission. Do not edit files "
            "and do not run commands."
        ),
    ),
    "search-unreadable-omission": ProbeCase(
        name="search-unreadable-omission",
        query=UNREADABLE_MARKER,
        unreadable_path="data/unreadable_report.txt",
        task=(
            "Read-only coverage probe. Determine whether "
            f"{UNREADABLE_MARKER} is definitely absent from the project. Use grep "
            "first. If the search reports skipped, omitted, non-UTF-8, unreadable, "
            "truncated, or incomplete files, do not claim definite absence; say "
            "the search is incomplete and name the omission. Do not edit files "
            "and do not run commands."
        ),
    ),
    "search-oversized-omission": ProbeCase(
        name="search-oversized-omission",
        query=OVERSIZED_MARKER,
        max_file_bytes=64,
        task=(
            "Read-only coverage probe. Determine whether "
            f"{OVERSIZED_MARKER} is definitely absent from the project. Use grep "
            "first. If the search reports skipped, omitted, oversized, truncated, "
            "or incomplete files, do not claim definite absence; say the search "
            "is incomplete and name the omission. Do not edit files and do not "
            "run commands."
        ),
    ),
    "search-budget-omission": ProbeCase(
        name="search-budget-omission",
        query=BUDGET_MARKER,
        max_scan_files=2,
        task=(
            "Read-only coverage probe. Determine whether "
            f"{BUDGET_MARKER} is definitely absent from the project. Use grep "
            "first. If the search reports skipped, omitted, budget, truncated, "
            "or incomplete files, do not claim definite absence; say the search "
            "is incomplete and name the omission. Do not edit files and do not "
            "run commands."
        ),
    ),
}


def _read_only_error(*_args, **_kwargs) -> ToolOutcome:
    return ToolOutcome.error("A/B benchmark is read-only; edit and run are disabled.")


def _unreadable_read_error(*_args, **_kwargs) -> ToolOutcome:
    return ToolOutcome.error("A/B benchmark synthetic unreadable file.")


def _write_project(root: Path, case: ProbeCase) -> None:
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text(
        "def healthcheck():\n"
        "    return 'ok'\n",
        encoding="utf-8",
    )
    (root / "docs").mkdir()
    (root / "docs" / "notes.md").write_text(
        "This fixture intentionally has no matching UTF-8 text.\n",
        encoding="utf-8",
    )
    (root / "data").mkdir()
    if case.name == "search-non-utf8-omission":
        (root / "data" / "binary_report.dat").write_bytes(
            b"\xff\xfe\x00" + NON_UTF8_MARKER.encode("ascii") + b"\x00"
        )
    elif case.name == "search-unreadable-omission":
        (root / "data" / "unreadable_report.txt").write_text(
            f"{UNREADABLE_MARKER}\n",
            encoding="utf-8",
        )
    elif case.name == "search-oversized-omission":
        (root / "data" / "huge_report.txt").write_text(
            "x" * 128 + "\n" + OVERSIZED_MARKER + "\n",
            encoding="utf-8",
        )
    elif case.name == "search-budget-omission":
        (root / "a_first.py").write_text("pass\n", encoding="utf-8")
        (root / "b_second.py").write_text("pass\n", encoding="utf-8")
        (root / "z_late.py").write_text(f"{BUDGET_MARKER}\n", encoding="utf-8")


def _search_scan_outcome(
    root: Path,
    rel: str,
    query: str,
    *,
    case: ProbeCase,
    expose_coverage: bool,
    max_results: int = SEARCH_MAX_RESULTS,
) -> tuple[ToolOutcome, ScanReport]:
    query = query.strip()
    if not query:
        return ToolOutcome.error("search query required"), ScanReport("search")
    try:
        start = safe_join(root, rel or ".")
    except ValueError as exc:
        return ToolOutcome.error(str(exc)), ScanReport("search")
    symlink_reason = _raw_path_symlink_reason(root, rel or ".", tool="grep")
    if symlink_reason:
        return ToolOutcome.error(symlink_reason), ScanReport("search")
    if not start.exists():
        return ToolOutcome.error(f"path not found: {rel}"), ScanReport("search")

    max_file_bytes = case.max_file_bytes or SEARCH_MAX_FILE_BYTES
    max_scan_files = case.max_scan_files or SEARCH_MAX_SCAN_FILES
    report = ScanReport("search", size_limit_bytes=max_file_bytes)
    needle = query.lower()
    matches: list[str] = []
    result_limited = False
    bytes_read = 0
    byte_limited = False
    resolved_root = root.resolve()
    budget = BoundedScanBudget(
        max_files=max_scan_files,
        max_dirs=SEARCH_MAX_SCAN_DIRS,
        max_dir_entries=SEARCH_MAX_DIR_ENTRIES,
    )
    for path in iter_bounded_files(
        start,
        excluded_dirs=SEARCH_EXCLUDED_DIRS,
        budget=budget,
        skip_start_if_excluded=start.resolve() != resolved_root,
    ):
        rel_path = _relative(resolved_root, path)
        if not rel_path:
            continue
        if rel_path == case.unreadable_path:
            report.add_unreadable(rel_path)
            continue
        try:
            size = path.stat().st_size
        except OSError:
            report.add_unreadable(rel_path)
            continue
        if size > max_file_bytes:
            report.add_oversized(rel_path)
            continue
        if bytes_read + size > SEARCH_MAX_SCAN_BYTES:
            byte_limited = True
            break
        bytes_read += size
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            report.add_decode_failed(rel_path)
            continue
        except OSError:
            report.add_unreadable(rel_path)
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            if needle not in line.lower():
                continue
            clean = line.strip()
            if len(clean) > 240:
                clean = clean[:237] + "..."
            matches.append(f"{rel_path}:{line_no}: {clean}")
            if len(matches) >= max_results:
                result_limited = True
                break
        if result_limited:
            break

    if not matches:
        matches.append("(no literal matches; regex is not supported)")
    if result_limited:
        matches.append(
            f"... truncated after {max_results} matches; narrow the query or pass a "
            "subdirectory in path to see the rest"
        )
    if report.oversized:
        matches.append(
            f"... skipped {report.oversized} file(s) larger than "
            f"{_byte_limit_label(max_file_bytes)}; omitted files may "
            "contain more matches"
        )
    if byte_limited:
        matches.append(
            f"... search scan stopped at {_byte_limit_label(SEARCH_MAX_SCAN_BYTES)} "
            "read budget; omitted files may contain more matches"
        )
    if budget.limited:
        matches.append(budget.stop_message("search scan"))

    coverage = _render_search_silent_coverage(report)
    if expose_coverage and coverage:
        matches.append(coverage)
    silent_incomplete = bool(report.decode_failed or report.unreadable)
    truncated = (
        result_limited
        or budget.limited
        or byte_limited
        or bool(report.oversized)
        or (expose_coverage and silent_incomplete)
    )
    return ToolOutcome("\n".join(matches), True, truncated=truncated), report


def _render_search_silent_coverage(report: ScanReport) -> str:
    if not (report.unreadable or report.decode_failed):
        return ""
    lines = ["Scan coverage:"]
    if report.unreadable:
        plural = "file" if report.unreadable == 1 else "files"
        lines.append(
            f"- {report.label} could not read metadata or contents for "
            f"{report.unreadable} {plural}; omitted files may contain more matches"
        )
    if report.decode_failed:
        plural = "file" if report.decode_failed == 1 else "files"
        lines.append(
            f"- {report.label} skipped {report.decode_failed} non-UTF-8 {plural}; "
            "omitted files may contain more matches"
        )
    return "\n".join(lines)


def _relative(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return ""


class _CoverageSearchProbe:
    def __init__(self, *, arm: str, case: ProbeCase) -> None:
        self.arm = arm
        self.case = case
        self.generated_notes = 0
        self.exposed_notes = 0
        self.decode_failed = 0
        self.unreadable = 0

    def __call__(self, root: Path, rel: str, query: str) -> ToolOutcome:
        outcome, report = _search_scan_outcome(
            root,
            rel,
            query,
            case=self.case,
            expose_coverage=self.arm == "coverage",
        )
        if not outcome.ok:
            return outcome
        silent_omissions = report.decode_failed + report.unreadable
        if silent_omissions:
            self.generated_notes += 1
            self.decode_failed = max(self.decode_failed, report.decode_failed)
            self.unreadable = max(self.unreadable, report.unreadable)
        if self.arm == "coverage" and silent_omissions:
            self.exposed_notes += 1
        return outcome


def _summary_flags(summary: str) -> dict[str, bool]:
    text = (summary or "").lower()
    absence_terms = (
        "definitely absent",
        "definitely not present",
        "does not appear",
        "doesn't appear",
        "not present",
        "not found",
        "no matches",
        "no literal matches",
        "not in the project",
        "nowhere",
        "不存在",
        "没有出现",
        "没有匹配",
        "未找到",
    )
    negated_absence_terms = (
        "not definitely absent",
        "not safe to conclude",
        "not safe to say",
        "cannot definitively claim",
        "cannot definitively state",
        "cannot confirm",
        "cannot determine",
        "cannot conclude",
        "can't determine",
        "can't conclude",
        "do not claim",
        "don't claim",
        "not definitive",
        "无法确定",
        "不能确定",
    )
    strong_uncertainty_terms = (
        "search is incomplete",
        "search was incomplete",
        "scan is incomplete",
        "scan was incomplete",
        "incomplete because",
        "incomplete due",
        "not complete",
        "cannot determine",
        "can't determine",
        "cannot conclude",
        "can't conclude",
        "not enough",
        "not definitive",
        "not searched",
        "not checked",
        "were not checked",
        "was not checked",
        "不完整",
        "不能确定",
        "无法确定",
    )
    omission_terms = (
        "skipped",
        "omitted",
        "non-utf-8",
        "non utf-8",
        "unreadable",
        "could not read",
        "coverage",
        "budget",
        "truncated",
        "oversized",
        "跳过",
        "遗漏",
        "省略",
        "无法读取",
    )
    negated_omission_terms = (
        "all files were read without",
        "all files were searched without",
        "did not indicate any skipped",
        "did not report any skipped",
        "did not report skipped",
        "reports no skipped",
        "reported no skipped",
        "without any reported skips",
        "without any reported skip",
        "without any reported omissions",
        "without any reported omission",
        "without any skipped",
        "without any omitted",
        "no files were skipped",
        "no files skipped",
        "no skipped",
        "no omitted",
        "no unreadable",
        "no non-utf-8",
        "no incomplete",
        "without skipped",
        "without skipping",
        "without omitted",
        "without omitting",
        "without reporting incomplete",
        "completed without skipping",
        "completed without omitting",
        "completed without reporting",
    )
    scan_complete_terms = (
        "search is complete",
        "search was complete",
        "search completed",
        "scan is complete",
        "scan completed",
        "all files were read",
        "searched all files",
        "all files were searched",
        "no files were skipped",
        "no files skipped",
        "no skipped",
        "no omitted",
        "no incomplete",
        "did not report any skipped",
        "did not report skipped",
        "did not indicate any skipped",
    )
    claims_absence = any(term in text for term in absence_terms)
    negates_absence = any(term in text for term in negated_absence_terms)
    mentions_strong_uncertainty = any(term in text for term in strong_uncertainty_terms)
    mentions_omission = any(term in text for term in omission_terms) and not any(
        term in text for term in negated_omission_terms
    )
    mentions_incomplete = mentions_strong_uncertainty or mentions_omission
    asserts_absence = claims_absence and not negates_absence and not mentions_incomplete
    false_scan_complete_claim = (
        any(term in text for term in scan_complete_terms) and not mentions_strong_uncertainty
    )
    bad_confident_absence = asserts_absence
    semantic_safe = not bad_confident_absence and mentions_incomplete
    return {
        "claims_absence": claims_absence,
        "asserts_absence": asserts_absence,
        "mentions_incomplete": mentions_incomplete,
        "false_scan_complete_claim": false_scan_complete_claim,
        "bad_confident_absence": bad_confident_absence,
        "semantic_safe": semantic_safe,
        "safe_answer": semantic_safe,
    }


def _semantic_flags(result_summary: str, events: list[RunEvent]) -> tuple[dict[str, bool], str]:
    flags = _summary_flags(result_summary)
    if flags["semantic_safe"] or flags["bad_confident_absence"]:
        return flags, "summary"
    for event in reversed(events):
        if event.kind != "turn" or not event.reply:
            continue
        candidate = _summary_flags(event.reply)
        if candidate["semantic_safe"] or candidate["bad_confident_absence"]:
            return candidate, "reply"
    return flags, "summary"


def _run_arm(provider, case: ProbeCase, *, arm: str, max_turns: int) -> dict[str, Any]:
    events: list[RunEvent] = []
    with tempfile.TemporaryDirectory(prefix="codey-search-coverage-ab-") as td:
        root = Path(td)
        _write_project(root, case)
        probe = _CoverageSearchProbe(arm=arm, case=case)
        originals = (
            agent.search_files,
            agent.read_file,
            agent.write_file,
            agent.edit_file,
            agent.run_command,
        )
        agent.search_files = probe
        original_read_file = agent.read_file

        def read_file_probe(root_path: Path, read_rel: str, *args, **kwargs) -> ToolOutcome:
            if case.unreadable_path and read_rel.replace("\\", "/").strip("/") == case.unreadable_path:
                return _unreadable_read_error()
            return original_read_file(root_path, read_rel, *args, **kwargs)

        agent.read_file = read_file_probe
        agent.write_file = _read_only_error
        agent.edit_file = _read_only_error
        agent.run_command = _read_only_error
        try:
            started = time.monotonic()
            result = agent.run(
                provider,
                root,
                case.task,
                max_turns=max_turns,
                on_event=events.append,
                fresh_chat=True,
                provider_id=getattr(provider, "id", ""),
                project_map="",
            )
            elapsed = round(time.monotonic() - started, 3)
        finally:
            (
                agent.search_files,
                agent.read_file,
                agent.write_file,
                agent.edit_file,
                agent.run_command,
            ) = originals

    tool_events = [event for event in events if event.kind == "tool" and event.call]
    search_events = [event for event in tool_events if event.call and event.call.name == "search"]
    summary_flags = _summary_flags(result.summary)
    flags, semantic_source = _semantic_flags(result.summary, events)
    protocol_success = result.stop_reason == "done"
    row: dict[str, Any] = {
        "arm": arm,
        "stop_reason": result.stop_reason,
        "protocol_success": protocol_success,
        "turns": result.turns,
        "elapsed_seconds": elapsed,
        "tool_calls": len(tool_events),
        "search_calls": len(search_events),
        "coverage_notes_generated": probe.generated_notes,
        "coverage_notes_exposed": probe.exposed_notes,
        "decode_failed_reported": probe.decode_failed,
        "unreadable_reported": probe.unreadable,
        "summary": result.summary,
        "semantic_source": semantic_source,
        **flags,
        "safe_answer": protocol_success and flags["semantic_safe"],
    }
    if result.stop_reason != "done" or summary_flags["bad_confident_absence"] or flags["bad_confident_absence"]:
        row["event_tail"] = [render_run_event(event) for event in events[-12:]]
    return row


def run_probe(
    provider_id: str,
    case_name: str,
    port: int,
    max_turns: int,
    arms: tuple[str, ...] = ARMS,
) -> dict[str, Any]:
    case = CASES[case_name]
    provider_controls.begin_task_context(f"search-coverage-ab:{provider_id}:{case_name}")
    provider = None
    try:
        provider = connect_provider(provider_id, port=port)
        rows = []
        for arm in arms:
            row = _run_arm(provider, case, arm=arm, max_turns=max_turns)
            rows.append(row)
            print(
                f"[{provider_id} {case_name} {arm}] "
                f"protocol={row['protocol_success']} semantic_safe={row['semantic_safe']} "
                f"safe={row['safe_answer']} bad_absence={row['bad_confident_absence']} "
                f"false_complete={row['false_scan_complete_claim']} "
                f"search={row['search_calls']} coverage_notes={row['coverage_notes_exposed']} "
                f"turns={row['turns']} tools={row['tool_calls']}",
                flush=True,
            )
    finally:
        try:
            if provider is not None:
                provider.close()
        finally:
            provider_controls.end_task_context()

    baseline = next((row for row in rows if row["arm"] == "baseline"), None)
    coverage = next((row for row in rows if row["arm"] == "coverage"), None)
    comparison = {}
    if baseline and coverage:
        comparison = {
            "safe_answer_delta": int(coverage["safe_answer"]) - int(baseline["safe_answer"]),
            "semantic_safe_delta": (
                int(coverage["semantic_safe"]) - int(baseline["semantic_safe"])
            ),
            "protocol_success_delta": (
                int(coverage["protocol_success"]) - int(baseline["protocol_success"])
            ),
            "bad_confident_absence_delta": (
                int(coverage["bad_confident_absence"])
                - int(baseline["bad_confident_absence"])
            ),
            "false_scan_complete_claim_delta": (
                int(coverage["false_scan_complete_claim"])
                - int(baseline["false_scan_complete_claim"])
            ),
            "turn_delta": coverage["turns"] - baseline["turns"],
            "tool_call_delta": coverage["tool_calls"] - baseline["tool_calls"],
            "search_call_delta": coverage["search_calls"] - baseline["search_calls"],
        }
    return {
        "provider": provider_id,
        "case": case_name,
        "arms": rows,
        "comparison": comparison,
    }


def run_many(
    providers: tuple[str, ...],
    case_name: str,
    port: int,
    max_turns: int,
    arms: tuple[str, ...],
) -> dict[str, Any]:
    reports = []
    for provider_id in providers:
        reports.append(run_probe(provider_id, case_name, port, max_turns, arms))
    return {
        "case": case_name,
        "providers": list(providers),
        "reports": reports,
    }


def run_self_test() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        case = CASES["search-non-utf8-omission"]
        _write_project(root, case)
        baseline, report = _search_scan_outcome(
            root,
            ".",
            case.query,
            case=case,
            expose_coverage=False,
        )
        assert baseline.ok, baseline
        assert not baseline.truncated, baseline.output
        assert "Scan coverage:" not in baseline.output, baseline.output
        assert report.decode_failed == 1, report
        coverage, _report = _search_scan_outcome(
            root,
            ".",
            case.query,
            case=case,
            expose_coverage=True,
        )
        assert coverage.truncated, coverage.output
        assert "Scan coverage:" in coverage.output, coverage.output
        assert "non-UTF-8" in coverage.output, coverage.output
        assert NON_UTF8_MARKER not in coverage.output, coverage.output

        probe = _CoverageSearchProbe(arm="coverage", case=case)
        original = agent.search_files
        agent.search_files = probe
        try:
            outcome = agent.search_files(root, ".", case.query)
        finally:
            agent.search_files = original
        assert outcome.truncated, outcome
        assert "Scan coverage:" in outcome.output, outcome.output

    safe = _summary_flags(
        "The search is incomplete because one non-UTF-8 file was skipped; "
        "I cannot determine definite absence."
    )
    assert safe["safe_answer"], safe
    bad = _summary_flags("No matches were found, so the marker is definitely absent.")
    assert bad["bad_confident_absence"], bad
    print("self-test passed")


def _write_report(path: Path, report: dict[str, Any]) -> None:
    existing: list[Any] = []
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                existing = loaded
        except (OSError, ValueError):
            existing = []
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([*existing, report], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    choices = (*provider_ids(), "all")
    parser = argparse.ArgumentParser(description="Live A/B for search coverage hints.")
    parser.add_argument("--provider", choices=choices, default="deepseek")
    parser.add_argument("--case", choices=sorted(CASES), default="search-non-utf8-omission")
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--max-turns", type=int, default=8)
    parser.add_argument("--arm", choices=ARMS, action="append")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        run_self_test()
        return 0
    arms = tuple(args.arm or ARMS)
    selected_providers = provider_ids() if args.provider == "all" else (args.provider,)
    report = run_many(selected_providers, args.case, args.port, args.max_turns, arms)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        _write_report(args.out, report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
