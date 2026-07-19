"""Live A/B probe for ChangeSet anchored review prompts.

This is intentionally outside normal pytest collection. It asks one live web
provider at a time to review fixed diffs with either the old path-only prompt
shape or the current ChangeSet-summary prompt shape.
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
from codey.review import (
    ReviewResult,
    parse_review_with_repair,
    render_review_prompt,
)


ARMS = ("baseline", "current")
DEFAULT_OUTPUT = Path(tempfile.gettempdir()) / "codey-changeset-review-ab.json"
MAX_REVIEW_DIFF_CHARS = 60_000
MAX_FIELD_CHARS = 2_000
MAX_REVIEW_LOG_CHARS = 8_000


@dataclass(frozen=True)
class ReviewCase:
    name: str
    task: str
    writer_summary: str
    changes: dict[str, Any]
    expected_paths: tuple[str, ...]
    issue_terms: tuple[str, ...]
    min_issue_terms: int = 2


CASES = (
    ReviewCase(
        name="boolean-guard",
        task=(
            "Apply a coupon discount only when a coupon exists and the coupon is "
            "active. Keep calls with coupon=None safe."
        ),
        writer_summary="Updated the coupon guard in src/cart.py.",
        changes={
            "ok": True,
            "mode": "git",
            "changed_count": 1,
            "files": [
                {
                    "path": "src/cart.py",
                    "status": "M",
                    "additions": 1,
                    "deletions": 1,
                }
            ],
            "diff": (
                "diff --git a/src/cart.py b/src/cart.py\n"
                "--- a/src/cart.py\n"
                "+++ b/src/cart.py\n"
                "@@ -4,7 +4,7 @@ def total(price, coupon=None):\n"
                "     subtotal = price\n"
                "-    if coupon and coupon.active:\n"
                "+    if coupon or coupon.active:\n"
                "         subtotal -= coupon.discount\n"
                "     return subtotal\n"
            ),
        },
        expected_paths=("src/cart.py",),
        issue_terms=("coupon", "or", "and", "none", "active", "attribute"),
        min_issue_terms=2,
    ),
    ReviewCase(
        name="rename-lost-clamp",
        task=(
            "Rename the discount helper to src/discounts.py while preserving the "
            "old behavior: percentages must stay between 0 and 50."
        ),
        writer_summary="Renamed the helper and simplified normalize_percent.",
        changes={
            "ok": True,
            "mode": "git",
            "changed_count": 1,
            "files": [
                {
                    "path": "src/legacy_discount.py -> src/discounts.py",
                    "status": "R",
                    "additions": 1,
                    "deletions": 1,
                }
            ],
            "diff": (
                "diff --git a/src/legacy_discount.py b/src/discounts.py\n"
                "similarity index 83%\n"
                "rename from src/legacy_discount.py\n"
                "rename to src/discounts.py\n"
                "--- a/src/legacy_discount.py\n"
                "+++ b/src/discounts.py\n"
                "@@ -1,4 +1,4 @@\n"
                " def normalize_percent(value):\n"
                "-    return min(50, max(0, value))\n"
                "+    return max(0, value)\n"
            ),
        },
        expected_paths=(
            "src/discounts.py",
            "src/legacy_discount.py -> src/discounts.py",
        ),
        issue_terms=("50", "cap", "clamp", "limit", "maximum", "min"),
        min_issue_terms=2,
    ),
)


def _clip(text: object, limit: int = MAX_FIELD_CHARS) -> str:
    value = "" if text is None else str(text)
    value = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(value) <= limit:
        return value
    return value[:limit].rstrip() + "\n[truncated]"


def _legacy_review_prompt(
    *,
    project: str,
    task: str,
    writer_summary: str,
    changes: dict[str, Any],
    recent_log: str = "",
) -> str:
    files = changes.get("files") if isinstance(changes.get("files"), list) else []
    file_lines: list[str] = []
    for file in files[:20]:
        if not isinstance(file, dict):
            continue
        path = _clip(file.get("path"), 400)
        status = _clip(file.get("status") or "M", 20)
        additions = int(file.get("additions") or 0)
        deletions = int(file.get("deletions") or 0)
        file_lines.append(f"- {status} {path} +{additions} -{deletions}")
    changed_files = "\n".join(file_lines) if file_lines else "(not listed)"
    raw_diff = "" if changes.get("diff") is None else str(changes.get("diff"))
    diff_was_truncated = (
        bool(changes.get("truncated")) or len(raw_diff) > MAX_REVIEW_DIFF_CHARS
    )
    diff = _clip(raw_diff, MAX_REVIEW_DIFF_CHARS)
    diff_note = (
        "Diff truncation note: the diff was truncated before review. Review only "
        "the visible diff and explicitly avoid assuming omitted hunks are clean.\n\n"
        if diff_was_truncated
        else ""
    )
    log = _clip(recent_log, MAX_REVIEW_LOG_CHARS) or "(no recent tool log)"
    return (
        "You are a careful code reviewer. Review the writer model's completed "
        "code change. You are read-only: do not ask to edit files directly.\n\n"
        "Only request changes for concrete correctness, test, integration, or "
        "user-visible issues. Do not request broad rewrites or style-only cleanup.\n\n"
        "Review whether the change satisfies the Original user task. If the task "
        "is incomplete in a user-visible way, return a concrete finding tied to "
        "the most relevant changed file.\n\n"
        "Every findings[].path must be copied from the Changed files list below. "
        "Do not invent filenames.\n\n"
        "Return only JSON. No analysis. No explanation. The first character "
        "must be { and the last character must be }.\n"
        "Return exactly one JSON object and no markdown fences:\n"
        '{"verdict":"approved","summary":"Looks good","findings":[]}\n'
        "or\n"
        '{"verdict":"changes_requested","summary":"One issue found","findings":'
        '[{"path":"<copy path from Changed files>","issue":"Concrete problem",'
        '"suggested_fix":"Small fix"}]}\n\n'
        f"Project: {project}\n\n"
        f"Original user task:\n{_clip(task, 6_000)}\n\n"
        f"Writer summary:\n{_clip(writer_summary, 2_000)}\n\n"
        f"Changed files:\n{changed_files}\n\n"
        f"Recent tool log:\n{log}\n\n"
        f"{diff_note}"
        f"Diff:\n{diff}\n"
    )


def _prompt_for(case: ReviewCase, arm: str) -> str:
    if arm == "current":
        return render_review_prompt(
            project="changeset-review-ab",
            task=case.task,
            writer_summary=case.writer_summary,
            changes=case.changes,
            recent_log="[agent] edit succeeded; no check result is relevant here",
        )
    return _legacy_review_prompt(
        project="changeset-review-ab",
        task=case.task,
        writer_summary=case.writer_summary,
        changes=case.changes,
        recent_log="[agent] edit succeeded; no check result is relevant here",
    )


def _result_text(result: ReviewResult, reply: str) -> str:
    parts = [result.summary, reply]
    for finding in result.findings:
        parts.extend(
            [
                finding.path,
                finding.issue,
                finding.suggested_fix,
                str(finding.hunk_index or ""),
                str(finding.new_line or ""),
                str(finding.old_line or ""),
            ]
        )
    return "\n".join(parts).lower()


def _score(case: ReviewCase, arm: str, result: ReviewResult, reply: str) -> dict[str, Any]:
    text = _result_text(result, reply)
    matched_terms = [term for term in case.issue_terms if term.lower() in text]
    issue_hit = len(matched_terms) >= case.min_issue_terms
    path_ok = any(finding.path in case.expected_paths for finding in result.findings)
    anchor_ok = any(
        finding.hunk_index is not None
        or finding.new_line is not None
        or finding.old_line is not None
        for finding in result.findings
    )
    verdict_ok = result.verdict == "changes_requested"
    return {
        "arm": arm,
        "verdict": result.verdict,
        "summary": result.summary,
        "finding_count": len(result.findings),
        "path_ok": path_ok,
        "issue_hit": issue_hit,
        "anchor_ok": anchor_ok,
        "matched_terms": matched_terms,
        "quality_score": (
            (2 if verdict_ok else 0)
            + (2 if issue_hit else 0)
            + (1 if path_ok else 0)
            + (2 if anchor_ok else 0)
        ),
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


def run_case(provider, provider_id: str, case: ReviewCase, arm: str, timeout: float) -> dict[str, Any]:
    started = time.monotonic()
    prompt = _prompt_for(case, arm)
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
                        f"path={row['path_ok']} issue={row['issue_hit']} "
                        f"anchor={row['anchor_ok']} elapsed={row['elapsed_seconds']}s"
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
            "path_ok_rate": round(
                sum(1 for row in arm_rows if row.get("path_ok")) / len(arm_rows),
                3,
            ),
            "issue_hit_rate": round(
                sum(1 for row in arm_rows if row.get("issue_hit")) / len(arm_rows),
                3,
            ),
            "anchor_ok_rate": round(
                sum(1 for row in arm_rows if row.get("anchor_ok")) / len(arm_rows),
                3,
            ),
        }
    return summary


def _write_output(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def self_test() -> int:
    rows: list[dict[str, Any]] = []
    for case in CASES:
        current_prompt = _prompt_for(case, "current")
        baseline_prompt = _prompt_for(case, "baseline")
        assert "ChangeSet Summary" in current_prompt
        assert "ChangeSet Summary" not in baseline_prompt
        assert "hunk_index" in current_prompt
        assert "hunk_index" not in baseline_prompt
        canned = (
            '{"verdict":"changes_requested","summary":"Bug found",'
            '"findings":[{"path":"'
            + case.expected_paths[0]
            + '","hunk_index":1,"new_line":2,'
            '"issue":"This violates '
            + " ".join(case.issue_terms[:2])
            + '","suggested_fix":"Fix it"}]}'
        )
        parsed = parse_review_with_repair(canned, lambda _repair: canned, changes=case.changes)
        rows.append(_score(case, "current", parsed, canned))
    assert all(row["anchor_ok"] for row in rows)
    assert all(row["issue_hit"] for row in rows)
    print("self-test passed")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=provider_ids(), help="provider to test")
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
    result = run_provider(
        args.provider,
        port=args.port,
        arms=arms,
        cases=cases,
        timeout=args.timeout,
    )
    result["summary"] = _summarize(result["rows"])
    _write_output(args.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
