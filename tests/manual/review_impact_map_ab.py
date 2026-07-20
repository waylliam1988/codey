"""Live A/B probe for the review-only Impact Map.

This is intentionally outside normal pytest collection. It compares the current
production review prompt without impact hints against the same production
prompt with a short bounded impact map. The fixture files are temporary; the
probe never changes Writer behavior, tools, UI, or runtime permissions.
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

# ruff: noqa: E402 - direct script execution must add the repository root first.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codey.providers.registry import connect_provider, provider_ids
from codey.review import ReviewResult, parse_review_with_repair, render_review_prompt
from codey.review_impact_map import render_review_impact_map
from codey.verification_map import render_verification_map


ARMS = ("current", "impact_map")
DEFAULT_OUTPUT = Path(tempfile.gettempdir()) / "codey-review-impact-map-ab.json"
MAX_REPLY_CHARS = 4_000
MAX_FIELD_CHARS = 2_000


@dataclass(frozen=True)
class ReviewCase:
    name: str
    files: dict[str, str]
    task: str
    writer_summary: str
    changes: dict[str, Any]
    expected_verdict: str
    changed_paths: tuple[str, ...]
    issue_terms: tuple[str, ...] = ()
    min_issue_terms: int = 2
    expected_refs: tuple[str, ...] = ()
    expected_tests: tuple[str, ...] = ()


CASES = (
    ReviewCase(
        name="python-missed-caller",
        files={
            "src/__init__.py": "",
            "src/discounts.py": (
                "def normalize_discount(percent):\n"
                "    return max(0, min(50, percent))\n"
            ),
            "src/checkout.py": (
                "from src.discounts import normalize_discount\n\n"
                "def final_total(total, percent):\n"
                "    return total * (100 - normalize_discount(percent)) / 100\n"
            ),
            "tests/test_checkout.py": (
                "from src.checkout import final_total\n\n"
                "def test_discount_is_clamped():\n"
                "    assert final_total(100, 80) == 50\n"
            ),
        },
        task=(
            "Rename normalize_discount to clamp_discount throughout the project. "
            "Keep all callers working and preserve the existing clamp behavior."
        ),
        writer_summary="Renamed the function in src/discounts.py.",
        changes={
            "ok": True,
            "mode": "git",
            "changed_count": 1,
            "files": [
                {"path": "src/discounts.py", "status": "M", "additions": 1, "deletions": 1}
            ],
            "diff": (
                "diff --git a/src/discounts.py b/src/discounts.py\n"
                "--- a/src/discounts.py\n"
                "+++ b/src/discounts.py\n"
                "@@ -1,2 +1,2 @@\n"
                "-def normalize_discount(percent):\n"
                "+def clamp_discount(percent):\n"
                "     return max(0, min(50, percent))\n"
            ),
        },
        expected_verdict="changes_requested",
        changed_paths=("src/discounts.py",),
        issue_terms=("normalize_discount", "clamp_discount", "checkout", "caller", "import"),
        min_issue_terms=2,
        expected_refs=("src/checkout.py",),
    ),
    ReviewCase(
        name="ts-exported-caller-and-test",
        files={
            "src/format.ts": (
                "export function formatTotal(cents: number): string {\n"
                "  return `$${(cents / 100).toFixed(2)}`;\n"
                "}\n"
            ),
            "src/view.ts": (
                "import { formatTotal } from './format';\n\n"
                "export function renderTotal(cents: number): string {\n"
                "  return `Total: ${formatTotal(cents)}`;\n"
                "}\n"
            ),
            "tests/format.test.ts": (
                "import { formatTotal } from '../src/format';\n\n"
                "test('formats cents', () => {\n"
                "  expect(formatTotal(1234)).toBe('$12.34');\n"
                "});\n"
            ),
        },
        task=(
            "Rename the exported formatTotal helper to formatCurrency throughout "
            "the app, including callers and tests."
        ),
        writer_summary="Renamed the exported helper in src/format.ts.",
        changes={
            "ok": True,
            "mode": "git",
            "changed_count": 1,
            "files": [
                {"path": "src/format.ts", "status": "M", "additions": 1, "deletions": 1}
            ],
            "diff": (
                "diff --git a/src/format.ts b/src/format.ts\n"
                "--- a/src/format.ts\n"
                "+++ b/src/format.ts\n"
                "@@ -1,3 +1,3 @@\n"
                "-export function formatTotal(cents: number): string {\n"
                "+export function formatCurrency(cents: number): string {\n"
                "   return `$${(cents / 100).toFixed(2)}`;\n"
                " }\n"
            ),
        },
        expected_verdict="changes_requested",
        changed_paths=("src/format.ts",),
        issue_terms=("formatTotal", "formatCurrency", "view", "test", "caller", "import"),
        min_issue_terms=2,
        expected_refs=("src/view.ts",),
        expected_tests=("tests/format.test.ts",),
    ),
    ReviewCase(
        name="private-helper-contained",
        files={
            "src/math.py": (
                "def _normalize(value):\n"
                "    return max(0, value)\n\n"
                "def score(value):\n"
                "    return _normalize(value) + 1\n"
            ),
        },
        task=(
            "Rename the private helper _normalize to _clamp_value inside src/math.py. "
            "No public behavior should change."
        ),
        writer_summary="Renamed the private helper and its local call.",
        changes={
            "ok": True,
            "mode": "git",
            "changed_count": 1,
            "files": [
                {"path": "src/math.py", "status": "M", "additions": 2, "deletions": 2}
            ],
            "diff": (
                "diff --git a/src/math.py b/src/math.py\n"
                "--- a/src/math.py\n"
                "+++ b/src/math.py\n"
                "@@ -1,5 +1,5 @@\n"
                "-def _normalize(value):\n"
                "+def _clamp_value(value):\n"
                "     return max(0, value)\n\n"
                " def score(value):\n"
                "-    return _normalize(value) + 1\n"
                "+    return _clamp_value(value) + 1\n"
            ),
        },
        expected_verdict="approved",
        changed_paths=("src/math.py",),
    ),
)


def _clip(text: object, limit: int = MAX_FIELD_CHARS) -> str:
    value = "" if text is None else str(text)
    value = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(value) <= limit:
        return value
    return value[:limit].rstrip() + "\n[truncated]"


def _write_fixture(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _verification_map(root: Path, case: ReviewCase) -> str:
    try:
        return render_verification_map(root, case.changes).strip()
    except Exception:
        return ""


def _prompt_for(root: Path, case: ReviewCase, arm: str) -> str:
    verification_map = _verification_map(root, case)
    impact_map = render_review_impact_map(root, case.changes) if arm == "impact_map" else ""
    return render_review_prompt(
        project="review-impact-map-ab",
        task=case.task,
        writer_summary=case.writer_summary,
        changes=case.changes,
        recent_log="[agent] edit succeeded; no successful check result followed",
        verification_map=verification_map,
        review_impact_map=impact_map,
    )


def _result_text(result: ReviewResult, reply: str) -> str:
    parts = [result.verdict, result.summary, reply]
    for finding in result.findings:
        parts.extend([
            finding.path,
            finding.issue,
            finding.suggested_fix,
            str(finding.hunk_index or ""),
            str(finding.new_line or ""),
            str(finding.old_line or ""),
        ])
    return "\n".join(parts).lower()


def _score(case: ReviewCase, arm: str, result: ReviewResult, reply: str) -> dict[str, Any]:
    text = _result_text(result, reply)
    matched_terms = [term for term in case.issue_terms if term.lower() in text]
    issue_hit = (
        case.expected_verdict == "approved"
        or len(matched_terms) >= case.min_issue_terms
    )
    path_ok = (
        case.expected_verdict == "approved"
        or any(finding.path in case.changed_paths for finding in result.findings)
    )
    affected_ref_applicable = bool(case.expected_refs)
    relevant_test_applicable = bool(case.expected_tests)
    affected_ref_mentioned = (
        affected_ref_applicable
        and all(ref.lower() in text for ref in case.expected_refs)
    )
    relevant_test_mentioned = (
        relevant_test_applicable
        and all(test.lower() in text for test in case.expected_tests)
    )
    verdict_ok = result.verdict == case.expected_verdict
    false_positive_review = (
        case.expected_verdict == "approved"
        and result.verdict == "changes_requested"
        and bool(result.findings)
    )
    if case.expected_verdict == "approved":
        quality = (4 if verdict_ok else 0) - (3 if false_positive_review else 0)
    else:
        quality = (
            (2 if verdict_ok else 0)
            + (2 if issue_hit else 0)
            + (1 if path_ok else 0)
            + (2 if affected_ref_mentioned else 0)
            + (1 if relevant_test_mentioned else 0)
        )
    return {
        "arm": arm,
        "expected_verdict": case.expected_verdict,
        "verdict": result.verdict,
        "summary": result.summary,
        "finding_count": len(result.findings),
        "path_ok": path_ok,
        "issue_hit": issue_hit,
        "affected_ref_applicable": affected_ref_applicable,
        "affected_ref_mentioned": affected_ref_mentioned,
        "relevant_test_applicable": relevant_test_applicable,
        "relevant_test_mentioned": relevant_test_mentioned,
        "false_positive_review": false_positive_review,
        "matched_terms": matched_terms,
        "quality_score": quality,
        "findings": [
            {
                "path": item.path,
                "issue": item.issue,
                "suggested_fix": item.suggested_fix,
                "hunk_index": item.hunk_index,
                "new_line": item.new_line,
                "old_line": item.old_line,
            }
            for item in result.findings
        ],
    }


def run_case(
    provider,
    provider_id: str,
    case: ReviewCase,
    arm: str,
    timeout: float,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="codey-review-impact-") as td:
        root = Path(td)
        _write_fixture(root, case.files)
        prompt = _prompt_for(root, case, arm)
        started = time.monotonic()
        provider.new_chat()
        reply = provider.send(prompt, timeout=timeout)
        parsed = parse_review_with_repair(
            reply,
            lambda repair: provider.send(repair, timeout=timeout),
            changes=case.changes,
        )
        row = {
            "provider": provider_id,
            "case": case.name,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "prompt_chars": len(prompt),
            "reply_chars": len(reply),
            "reply_preview": _clip(reply, MAX_REPLY_CHARS),
        }
        row.update(_score(case, arm, parsed, reply))
        return row


def run_provider(
    provider_id: str,
    *,
    port: int,
    arms: tuple[str, ...],
    cases: tuple[ReviewCase, ...],
    timeout: float,
) -> dict[str, Any]:
    provider = connect_provider(provider_id, port=port, open_if_missing=False)
    rows: list[dict[str, Any]] = []
    started = time.monotonic()
    try:
        for case in cases:
            for arm in arms:
                try:
                    row = run_case(provider, provider_id, case, arm, timeout)
                except Exception as exc:
                    row = {
                        "provider": provider_id,
                        "case": case.name,
                        "arm": arm,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                rows.append(row)
                if "error" in row:
                    print(f"[{provider_id} {case.name} {arm}] {row['error']}")
                else:
                    print(
                        f"[{provider_id} {case.name} {arm}] "
                        f"score={row['quality_score']} verdict={row['verdict']} "
                        f"issue={row['issue_hit']} ref={row['affected_ref_mentioned']} "
                        f"test={row['relevant_test_mentioned']} "
                        f"false_positive={row['false_positive_review']} "
                        f"elapsed={row['elapsed_seconds']}s"
                    )
    finally:
        provider.close()
    return {
        "provider": provider_id,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "rows": rows,
    }


def _selected_cases(names: list[str]) -> tuple[ReviewCase, ...]:
    if not names:
        return CASES
    wanted = set(names)
    result = tuple(case for case in CASES if case.name in wanted)
    missing = wanted - {case.name for case in result}
    if missing:
        raise SystemExit(f"unknown case(s): {', '.join(sorted(missing))}")
    return result


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {"arms": {}}
    for arm in ARMS:
        arm_rows = [row for row in rows if row.get("arm") == arm and "error" not in row]
        if not arm_rows:
            summary["arms"][arm] = {"count": 0}
            continue
        summary["arms"][arm] = {
            "count": len(arm_rows),
            "avg_quality_score": round(
                sum(float(row.get("quality_score") or 0) for row in arm_rows)
                / len(arm_rows),
                3,
            ),
            "issue_hit_rate": _rate(arm_rows, "issue_hit"),
            "affected_ref_rate": _conditional_rate(
                arm_rows,
                "affected_ref_mentioned",
                "affected_ref_applicable",
            ),
            "relevant_test_rate": _conditional_rate(
                arm_rows,
                "relevant_test_mentioned",
                "relevant_test_applicable",
            ),
            "false_positive_rate": _rate(arm_rows, "false_positive_review"),
            "avg_prompt_chars": round(
                sum(int(row.get("prompt_chars") or 0) for row in arm_rows)
                / len(arm_rows),
                1,
            ),
        }
    current = summary["arms"].get("current", {})
    impact = summary["arms"].get("impact_map", {})
    if current.get("count") and impact.get("count"):
        summary["deltas_vs_current"] = {
            "avg_quality_score": round(
                float(impact.get("avg_quality_score") or 0)
                - float(current.get("avg_quality_score") or 0),
                3,
            ),
            "affected_ref_rate": round(
                float(impact.get("affected_ref_rate") or 0)
                - float(current.get("affected_ref_rate") or 0),
                3,
            ),
            "relevant_test_rate": round(
                float(impact.get("relevant_test_rate") or 0)
                - float(current.get("relevant_test_rate") or 0),
                3,
            ),
            "false_positive_rate": round(
                float(impact.get("false_positive_rate") or 0)
                - float(current.get("false_positive_rate") or 0),
                3,
            ),
            "avg_prompt_chars": round(
                float(impact.get("avg_prompt_chars") or 0)
                - float(current.get("avg_prompt_chars") or 0),
                1,
            ),
        }
    return summary


def _rate(rows: list[dict[str, Any]], key: str) -> float:
    if not rows:
        return 0.0
    return round(sum(1 for row in rows if row.get(key)) / len(rows), 3)


def _conditional_rate(
    rows: list[dict[str, Any]],
    key: str,
    applicable_key: str,
) -> float:
    scoped = [row for row in rows if row.get(applicable_key)]
    if not scoped:
        return 0.0
    return round(sum(1 for row in scoped if row.get(key)) / len(scoped), 3)


def _write_output(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _self_test_row(case: ReviewCase, arm: str) -> dict[str, Any]:
    canned = (
        '{"verdict":"'
        + case.expected_verdict
        + '","summary":"checked",'
        '"findings":['
    )
    if case.expected_verdict == "changes_requested":
        canned += (
            '{"path":"'
            + case.changed_paths[0]
            + '","issue":"'
            + " ".join((*case.issue_terms[:2], *case.expected_refs, *case.expected_tests))
            + '","suggested_fix":"update affected callers"}'
        )
    canned += "]}"
    parsed = parse_review_with_repair(canned, lambda _repair: canned, changes=case.changes)
    return _score(case, arm, parsed, canned)


def self_test() -> int:
    with tempfile.TemporaryDirectory(prefix="codey-review-impact-self-") as td:
        root = Path(td)
        for case in CASES:
            _write_fixture(root, case.files)
            impact_map = render_review_impact_map(root, case.changes)
            current_prompt = _prompt_for(root, case, "current")
            impact_prompt = _prompt_for(root, case, "impact_map")
            assert "Review Impact Map" not in current_prompt
            assert "Review Impact Map" in impact_prompt
            assert "ChangeSet Summary" in impact_prompt
            assert "not coverage proof" in impact_map
            assert "return `" not in impact_map
            for expected in (*case.expected_refs, *case.expected_tests):
                if expected:
                    assert expected in impact_map
            row = _self_test_row(case, "impact_map")
            assert row["issue_hit"]
            if case.expected_verdict == "approved":
                assert not row["false_positive_review"]
    print("self-test passed")
    return 0


def main() -> int:
    choices = (*provider_ids(), "all")
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=choices, help="provider to test")
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--arm", choices=ARMS, action="append", help="arm to run; default both")
    parser.add_argument("--case", action="append", default=[], help="case name; default all")
    parser.add_argument("--timeout", type=float, default=120.0, help="per send timeout seconds")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        return self_test()
    if not args.provider:
        raise SystemExit("--provider is required unless --self-test is used")
    arms = tuple(args.arm or ARMS)
    cases = _selected_cases(args.case)
    provider_list = provider_ids() if args.provider == "all" else (args.provider,)
    provider_results = []
    all_rows: list[dict[str, Any]] = []
    started = time.monotonic()
    for provider_id in provider_list:
        result = run_provider(
            provider_id,
            port=args.port,
            arms=arms,
            cases=cases,
            timeout=args.timeout,
        )
        provider_results.append(result)
        all_rows.extend(result["rows"])
    payload = {
        "probe": "review_impact_map_ab",
        "providers": provider_list,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "summary": _summarize(all_rows),
        "provider_results": provider_results,
    }
    _write_output(args.output, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
