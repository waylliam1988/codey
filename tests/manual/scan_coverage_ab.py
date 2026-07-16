"""Live A/B for references coverage hints.

The baseline arm reconstructs the old low-level find_reference_hints output
without Writer coverage rendering. The coverage arm monkeypatches the Writer's
find_references tool result to append a short omission note when the bounded
lexical reference scan skipped oversized candidate files. The goal is to
measure whether live web providers avoid a confident "no references" answer
when the local scan was incomplete.
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
from codey.events import RunEvent, render_run_event
from codey.providers.registry import connect_provider, provider_ids
from codey.references import find_reference_hints
from codey.scan_report import render_scan_coverage
from codey.tool_runtime import ToolOutcome, safe_join


ARMS = ("baseline", "coverage")
DEFAULT_OUTPUT = Path(tempfile.gettempdir()) / "codey-scan-coverage-ab.json"
OVERSIZED_SOURCE_MARKER = "LEGACY_PROCESS_PAYMENT_CALL"


@dataclass(frozen=True)
class ProbeCase:
    name: str
    symbol: str
    task: str


CASES = {
    "references-oversized-omission": ProbeCase(
        name="references-oversized-omission",
        symbol="process_payment",
        task=(
            "Read-only coverage probe. Determine whether process_payment is "
            "definitely unused outside its defining file. Use find_references "
            "first. If the reference scan reports skipped, omitted, oversized, "
            "or incomplete files, do not claim definite unused; say the scan is "
            "incomplete and name the omission. Do not edit files and do not run "
            "commands."
        ),
    ),
}


def _read_only_error(*_args, **_kwargs) -> ToolOutcome:
    return ToolOutcome.error("A/B benchmark is read-only; edit and run are disabled.")


def _write_project(root: Path) -> None:
    (root / "payments.py").write_text(
        "def process_payment(order):\n"
        "    return {'charged': order['total']}\n",
        encoding="utf-8",
    )
    (root / "app.py").write_text(
        "def healthcheck():\n"
        "    return 'ok'\n",
        encoding="utf-8",
    )
    legacy = root / "legacy"
    legacy.mkdir()
    for index in range(60):
        (legacy / f"a_filler_{index:02d}.py").write_text(
            f"def filler_{index:02d}():\n"
            "    return None\n",
            encoding="utf-8",
        )
    padding = "# historical batch padding\n" * 25_000
    (legacy / "z_legacy_batch.py").write_text(
        padding
        + "from payments import process_payment\n\n"
        + "def replay(order):\n"
        + f"    # {OVERSIZED_SOURCE_MARKER}\n"
        + "    return process_payment(order)\n",
        encoding="utf-8",
    )


def _reference_scan_outcome(
    root: Path,
    rel: str,
    symbol: str,
    *,
    expose_coverage: bool,
) -> tuple[ToolOutcome, int, tuple[str, ...]]:
    try:
        start = safe_join(root, rel or ".")
    except ValueError:
        return ToolOutcome.error(f"path escapes project root: {rel}"), 0, ()
    if not start.exists():
        return ToolOutcome.error(f"path not found: {rel}"), 0, ()
    try:
        scan = find_reference_hints(root, start, symbol)
    except ValueError as exc:
        return ToolOutcome.error(str(exc)), 0, ()
    report = scan.report
    coverage = render_scan_coverage(report) if report is not None else ""
    output = f"{scan.output}\n{coverage}" if expose_coverage and coverage else scan.output
    incomplete = bool(report and report.incomplete)
    outcome = ToolOutcome(output, True, truncated=scan.truncated or (expose_coverage and incomplete))
    oversized = report.oversized if report is not None else 0
    examples = tuple(report.oversized_examples) if report is not None else ()
    return outcome, oversized, examples


class _CoverageReferencesProbe:
    def __init__(self, *, arm: str) -> None:
        self.arm = arm
        self.generated_notes = 0
        self.exposed_notes = 0
        self.oversized_files = 0
        self.examples: tuple[str, ...] = ()

    def __call__(self, root: Path, rel: str, symbol: str) -> ToolOutcome:
        outcome, oversized, examples = _reference_scan_outcome(
            root,
            rel,
            symbol,
            expose_coverage=self.arm == "coverage",
        )
        if not outcome.ok:
            return outcome
        if oversized:
            self.generated_notes += 1
            self.oversized_files = max(self.oversized_files, oversized)
            self.examples = examples
        if self.arm == "coverage" and oversized:
            self.exposed_notes += 1
        return outcome


def _summary_flags(summary: str) -> dict[str, bool]:
    text = (summary or "").lower()
    absence_terms = (
        "definitely unused",
        "unused outside",
        "no external",
        "no references",
        "only definition",
        "only appears",
        "not referenced",
        "没有引用",
        "无引用",
        "没有外部",
        "未被引用",
        "只在定义",
    )
    negated_absence_terms = (
        "not definitely unused",
        "not unused",
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
        "scan is incomplete",
        "scan was incomplete",
        "is incomplete because",
        "incomplete because",
        "incomplete due",
        "not complete",
        "cannot determine",
        "can't determine",
        "cannot conclude",
        "can't conclude",
        "not enough",
        "not definitive",
        "not safe to conclude",
        "not checked",
        "were not checked",
        "was not checked",
        "not searched",
        "不完整",
        "不能确定",
        "无法确定",
    )
    omission_terms = (
        "skipped",
        "omitted",
        "oversized",
        "large file",
        "coverage",
        "跳过",
        "遗漏",
        "省略",
        "大文件",
    )
    negated_omission_terms = (
        "did not indicate any skipped",
        "did not report any skipped",
        "did not report skipped",
        "reports no skipped",
        "reported no skipped",
        "no files were skipped",
        "no files skipped",
        "no skipped",
        "no omitted",
        "no oversized",
        "no incomplete",
        "without skipped",
        "without skipping",
        "without omitted",
        "without omitting",
        "without oversized",
        "without reporting oversized",
        "without reporting incomplete",
        "completed without skipping",
        "completed without omitting",
        "completed without reporting",
    )
    has_reference_terms = (
        "z_legacy_batch.py",
        "legacy_batch",
        "has a reference",
        "is referenced",
        "存在引用",
        "被引用",
    )
    scan_complete_terms = (
        "reference scan is complete",
        "scan is complete",
        "scan completed",
        "completed without skipping",
        "completed without omitting",
        "no files were skipped",
        "no files skipped",
        "no skipped",
        "no omitted",
        "no oversized",
        "no incomplete",
        "did not report any skipped",
        "did not report skipped",
        "did not indicate any skipped",
        "reports no skipped",
        "reported no skipped",
    )
    claims_absence = any(term in text for term in absence_terms)
    negates_absence = any(term in text for term in negated_absence_terms)
    mentions_strong_uncertainty = any(term in text for term in strong_uncertainty_terms)
    mentions_omission = any(term in text for term in omission_terms) and not any(
        term in text for term in negated_omission_terms
    )
    mentions_incomplete = mentions_strong_uncertainty or mentions_omission
    mentions_reference = any(term in text for term in has_reference_terms)
    asserts_absence = claims_absence and not negates_absence and not mentions_incomplete
    contradictory_absence_with_reference = asserts_absence and mentions_reference
    bad_confident_absence = asserts_absence and not mentions_reference
    false_scan_complete_claim = (
        any(term in text for term in scan_complete_terms) and not mentions_strong_uncertainty
    )
    semantic_safe = (
        not bad_confident_absence
        and not contradictory_absence_with_reference
        and (mentions_incomplete or mentions_reference)
    )
    return {
        "claims_absence": claims_absence,
        "asserts_absence": asserts_absence,
        "mentions_incomplete": mentions_incomplete,
        "mentions_hidden_reference": mentions_reference,
        "contradictory_absence_with_reference": contradictory_absence_with_reference,
        "false_scan_complete_claim": false_scan_complete_claim,
        "bad_confident_absence": bad_confident_absence,
        "semantic_safe": semantic_safe,
        "safe_answer": semantic_safe,
    }


def _semantic_flags(result_summary: str, events: list[RunEvent]) -> tuple[dict[str, bool], str]:
    flags = _summary_flags(result_summary)
    if flags["semantic_safe"] or flags["bad_confident_absence"] or flags["contradictory_absence_with_reference"]:
        return flags, "summary"
    for event in reversed(events):
        if event.kind != "turn" or not event.reply:
            continue
        candidate = _summary_flags(event.reply)
        if (
            candidate["semantic_safe"]
            or candidate["bad_confident_absence"]
            or candidate["contradictory_absence_with_reference"]
        ):
            return candidate, "reply"
    return flags, "summary"


def _run_arm(provider, case: ProbeCase, *, arm: str, max_turns: int) -> dict[str, Any]:
    events: list[RunEvent] = []
    with tempfile.TemporaryDirectory(prefix="codey-scan-coverage-ab-") as td:
        root = Path(td)
        _write_project(root)
        probe = _CoverageReferencesProbe(arm=arm)
        originals = (agent.find_references, agent.write_file, agent.edit_file, agent.run_command)
        agent.find_references = probe
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
                agent.find_references,
                agent.write_file,
                agent.edit_file,
                agent.run_command,
            ) = originals

    tool_events = [event for event in events if event.kind == "tool" and event.call]
    reference_events = [
        event for event in tool_events if event.call and event.call.name == "references"
    ]
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
        "references_calls": len(reference_events),
        "search_calls": len(search_events),
        "coverage_notes_generated": probe.generated_notes,
        "coverage_notes_exposed": probe.exposed_notes,
        "oversized_files_reported": probe.oversized_files,
        "skipped_path_examples": list(probe.examples),
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
    provider_controls.begin_task_context(f"scan-coverage-ab:{provider_id}:{case_name}")
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
                f"contradiction={row['contradictory_absence_with_reference']} "
                f"false_complete={row['false_scan_complete_claim']} "
                f"refs={row['references_calls']} search={row['search_calls']} "
                f"coverage_notes={row['coverage_notes_exposed']} "
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
            "contradictory_absence_with_reference_delta": (
                int(coverage["contradictory_absence_with_reference"])
                - int(baseline["contradictory_absence_with_reference"])
            ),
            "false_scan_complete_claim_delta": (
                int(coverage["false_scan_complete_claim"])
                - int(baseline["false_scan_complete_claim"])
            ),
            "turn_delta": coverage["turns"] - baseline["turns"],
            "tool_call_delta": coverage["tool_calls"] - baseline["tool_calls"],
            "references_call_delta": (
                coverage["references_calls"] - baseline["references_calls"]
            ),
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
        _write_project(root)
        outcome, oversized, examples = _reference_scan_outcome(
            root,
            ".",
            "process_payment",
            expose_coverage=True,
        )
        assert "Scan coverage:" in outcome.output, outcome.output
        assert "oversized" in outcome.output, outcome.output
        assert oversized == 1, oversized
        assert examples == ("legacy/z_legacy_batch.py",), examples
        assert OVERSIZED_SOURCE_MARKER not in outcome.output, outcome.output

        probe = _CoverageReferencesProbe(arm="coverage")
        original = agent.find_references
        agent.find_references = probe
        try:
            outcome = agent.find_references(root, ".", "process_payment")
        finally:
            agent.find_references = original
        assert outcome.truncated, outcome
        assert "Scan coverage:" in outcome.output, outcome.output
        assert OVERSIZED_SOURCE_MARKER not in outcome.output, outcome.output

    flags = _summary_flags(
        "The scan is incomplete because one oversized file was skipped; "
        "I cannot determine definite unused status."
    )
    assert flags["safe_answer"], flags
    assert not flags["bad_confident_absence"], flags
    bad = _summary_flags("No references were found; it is definitely unused outside payments.py.")
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
    parser = argparse.ArgumentParser(description="Live A/B for scan coverage hints.")
    parser.add_argument("--provider", choices=choices, default="deepseek")
    parser.add_argument("--case", choices=sorted(CASES), default="references-oversized-omission")
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
