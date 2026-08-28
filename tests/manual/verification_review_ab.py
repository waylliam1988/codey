"""Manual A/B for Verification Map influence on live web reviewers.

This is review-only: it never opens or mutates a local project. The manual
evidence path mirrors the 0.4 A/B spine: fixed result JSON, append-only
journal, optional archived transcripts, and a per-output manifest.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import time
from pathlib import Path
from typing import Any, Mapping

# ruff: noqa: E402 - direct script execution must add the repository root first.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codey.providers.registry import connect_provider, provider_ids
from codey.reviews.core import parse_review_with_repair, render_review_prompt
from tests.manual.ab_harness_common import (
    ArmRunLayout,
    ResultRowStore,
    TracingProvider,
    bind_row_evidence_refs,
    build_arm_manifest,
    classify_ab_failure,
    classify_provider_failure,
    open_journal_for_output,
    row_has_terminal_failure,
    timestamp,
    write_arm_manifest,
)
from tests.manual.ab_journal import ABJournalWriter

dataclass = dataclasses.dataclass

RESULTS_DIR = Path(__file__).parent / "results"
ARMS = ("baseline", "current")


@dataclass(frozen=True)
class ReviewCase:
    name: str
    project: str
    task: str
    writer_summary: str
    changes: dict[str, object]
    recent_log: str
    verification_map: str


AUTH_REVIEW_CASE = ReviewCase(
    name="auth_normalization_review",
    project="temporary-auth-project",
    task=(
        "Make normalize_username reject blank input and return a stripped, "
        "lowercase username. Keep existing callers working and verify the change."
    ),
    writer_summary="Implemented blank validation and normalization in src/auth.py.",
    changes={
        "ok": True,
        "changed_count": 1,
        "files": [{"path": "src/auth.py", "status": "M", "additions": 4, "deletions": 1}],
        "diff": (
            "diff --git a/src/auth.py b/src/auth.py\n"
            "--- a/src/auth.py\n+++ b/src/auth.py\n"
            "@@\n-def normalize_username(value):\n-    return value\n"
            "+def normalize_username(value):\n"
            "+    if not value.strip():\n+        raise ValueError('username required')\n"
            "+    return value.strip().lower()\n"
        ),
    },
    recent_log="[agent] edit src/auth.py succeeded; no run result followed",
    verification_map="""Verification Map (bounded candidates; not coverage proof):
Changed files:
- src/auth.py
Changed tests:
- (none)
Existing test candidates found locally (not necessarily changed):
- tests/test_auth.py: name corresponds to changed file src/auth.py [evidence: naming]
Observed successful checks after the latest edit:
- (none observed)
Broader check candidates (inspect relevance before requesting):
- python -m pytest [evidence: project manifest]
""",
)
CASES = (AUTH_REVIEW_CASE,)
CASE_BY_NAME = {case.name: case for case in CASES}


def _prompt_for(case: ReviewCase, arm: str) -> str:
    if arm not in ARMS:
        raise ValueError(f"unknown arm: {arm}")
    return render_review_prompt(
        project=case.project,
        task=case.task,
        writer_summary=case.writer_summary,
        changes=case.changes,
        recent_log=case.recent_log,
        verification_map=case.verification_map if arm == "current" else "",
    )


def _run_case(provider: TracingProvider, case: ReviewCase, arm: str) -> dict[str, Any]:
    started = time.monotonic()
    provider.new_chat()
    prompt = _prompt_for(case, arm)
    reply = provider.send(prompt)
    repair_turns = 0

    def send_repair(repair_prompt: str) -> str:
        nonlocal repair_turns
        repair_turns += 1
        return provider.send(repair_prompt)

    parsed = parse_review_with_repair(
        reply,
        send_repair,
        changes=case.changes,
    )
    reply_text = "\n".join(provider.replies)
    findings = [
        {
            "path": item.path,
            "issue": item.issue,
            "suggested_fix": item.suggested_fix,
            "hunk_index": item.hunk_index,
            "new_line": item.new_line,
            "old_line": item.old_line,
        }
        for item in parsed.findings
    ]
    return {
        "case": case.name,
        "arm": arm,
        "ok": True,
        "protocol_ok": True,
        "stop_reason": "done",
        "provider": "",
        "elapsed_s": round(time.monotonic() - started, 2),
        "verdict": parsed.verdict,
        "summary": parsed.summary,
        "findings": findings,
        "finding_count": len(findings),
        "repair_turns": repair_turns,
        "mentions_existing_test": "test_auth.py" in reply_text,
        "mentions_check": any(term in reply_text.lower() for term in ("pytest", "test", "check")),
        "send_count": provider.send_index,
        "reply_count": provider.reply_count,
        "prompt_chars": provider.prompt_chars,
        "reply_chars": provider.reply_chars,
    }


def _error_row(
    *,
    case: ReviewCase,
    arm: str,
    exc: BaseException,
    tracing_provider: TracingProvider,
    elapsed_s: float,
) -> dict[str, Any]:
    provider_failure = classify_provider_failure(
        sends=tracing_provider.send_index,
        replies=tracing_provider.reply_count,
        error=exc,
    )
    failure_class = classify_ab_failure(exc)
    return {
        "case": case.name,
        "arm": arm,
        "ok": False,
        "protocol_ok": False,
        "stop_reason": "error",
        "error": f"{type(exc).__name__}: {exc}",
        "provider_error_class": provider_failure,
        "codey_failure_class": failure_class,
        "provider_failure_class": provider_failure,
        "elapsed_s": round(elapsed_s, 2),
        "repair_turns": 0,
        "finding_count": 0,
        "send_count": tracing_provider.send_index,
        "reply_count": tracing_provider.reply_count,
        "prompt_chars": tracing_provider.prompt_chars,
        "reply_chars": tracing_provider.reply_chars,
    }


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_arm: dict[str, dict[str, Any]] = {}
    for row in rows:
        arm = str(row.get("arm") or "<unknown>")
        item = by_arm.setdefault(
            arm,
            {
                "total": 0,
                "protocol_ok": 0,
                "errors": 0,
                "approved": 0,
                "changes_requested": 0,
                "mentions_existing_test": 0,
                "mentions_check": 0,
                "finding_count": 0,
                "repair_turns": 0,
                "transcript_replayable": 0,
            },
        )
        item["total"] += 1
        if row.get("protocol_ok"):
            item["protocol_ok"] += 1
        if row_has_terminal_failure(row):
            item["errors"] += 1
        if row.get("verdict") == "approved":
            item["approved"] += 1
        if row.get("verdict") == "changes_requested":
            item["changes_requested"] += 1
        if row.get("mentions_existing_test"):
            item["mentions_existing_test"] += 1
        if row.get("mentions_check"):
            item["mentions_check"] += 1
        item["finding_count"] += int(row.get("finding_count") or 0)
        item["repair_turns"] += int(row.get("repair_turns") or 0)
        if row.get("transcript_replayable"):
            item["transcript_replayable"] += 1
    return {"rows": len(rows), "by_arm": by_arm}


def _ok(rows: list[dict[str, Any]], complete: bool) -> bool:
    return bool(
        complete
        and rows
        and not any(row_has_terminal_failure(row) for row in rows)
        and all(row.get("protocol_ok") for row in rows)
    )


def _aggregate_failure_class(rows: list[dict[str, Any]], key: str) -> str:
    for row in rows:
        value = str(row.get(key) or "").strip()
        if value and value != "none":
            return value
    return "none"


def _write_manifest(
    output: Path,
    *,
    provider_id: str,
    arms: tuple[str, ...],
    cases: tuple[ReviewCase, ...],
    max_turns: int,
    transcript_mode: str,
    payload: Mapping[str, Any],
    stop_reason: str,
    journal: ABJournalWriter | None = None,
    layout: ArmRunLayout | None = None,
) -> None:
    resolved_layout = layout or ArmRunLayout.for_output(output)
    rows = [dict(row) for row in payload.get("rows", []) if isinstance(row, Mapping)]
    write_arm_manifest(
        output,
        build_arm_manifest(
            suite="verification_review_ab",
            provider=provider_id,
            arms=arms,
            cases=[case.name for case in cases],
            max_turns=max_turns,
            journal_dir=resolved_layout.journal_dir if journal is not None else None,
            transcript_mode=transcript_mode if journal is not None else "off",
            started_at=str(payload.get("started_at") or ""),
            finished_at=str(payload.get("finished_at") or timestamp()),
            stop_reason=stop_reason,
            provider_error_class=_aggregate_failure_class(rows, "provider_error_class"),
            codey_failure_class=_aggregate_failure_class(rows, "codey_failure_class"),
            repo=ROOT,
        ),
    )


def run_live(
    provider_id: str,
    arm: str,
    port: int,
    *,
    output: Path | None = None,
    transcript_mode: str = "digest-only",
    rerun_failed: bool = False,
) -> dict[str, Any]:
    if arm not in ARMS:
        raise ValueError(f"unknown arm: {arm}")
    cases = CASES
    max_turns = 4
    RESULTS_DIR.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    out = output or RESULTS_DIR / f"verification_review_ab_{provider_id}_{arm}_{stamp}.json"
    layout = ArmRunLayout.for_output(out)
    store = ResultRowStore.open(
        out,
        probe="verification_review_ab",
        provider_id=provider_id,
        cases=cases,
        arms=(arm,),
        summarize=_summarize,
        ok=_ok,
    )
    pending = [
        (CASE_BY_NAME[case_name], pending_arm)
        for case_name, pending_arm, _repeat in store.pending_keys(
            cases=cases,
            arms=(arm,),
            rerun_failed=rerun_failed,
        )
    ]
    if not pending:
        finished_at = timestamp()
        store.write(complete=True, extra={"finished_at": finished_at})
        _write_manifest(
            out,
            provider_id=provider_id,
            arms=(arm,),
            cases=cases,
            max_turns=max_turns,
            transcript_mode=transcript_mode,
            payload=store.payload,
            stop_reason="already_complete",
            layout=layout,
        )
        print(f"[{provider_id} {arm}] no pending rows; use --rerun-failed or a new --output.", flush=True)
        return store.payload

    raw_provider = None
    journal: ABJournalWriter | None = None
    run_finished = False
    try:
        raw_provider = connect_provider(provider_id, port=port, open_if_missing=False)
        journal = open_journal_for_output(
            output=out,
            experiment_id="verification_review_ab",
            provider_id=provider_id,
            transcript_mode=transcript_mode,
            case_names=[case.name for case in cases],
            arms=(arm,),
            max_turns=max_turns,
            journal_dir=layout.journal_dir,
        )
        store.write(complete=False)
        for case, pending_arm in pending:
            started = time.monotonic()
            tracing_provider = TracingProvider(
                raw_provider,
                journal=journal,
                case=case.name,
                arm=pending_arm,
            )
            if journal is not None:
                journal.record_case_start(
                    case=case.name,
                    arm=pending_arm,
                    question_chars=len(case.task),
                )
            try:
                row = _run_case(tracing_provider, case, pending_arm)
            except Exception as exc:
                row = _error_row(
                    case=case,
                    arm=pending_arm,
                    exc=exc,
                    tracing_provider=tracing_provider,
                    elapsed_s=time.monotonic() - started,
                )
            row["provider"] = provider_id
            bind_row_evidence_refs(row, layout=layout, tracing_provider=tracing_provider)
            if journal is not None:
                journal.record_case_complete(
                    case=case.name,
                    arm=pending_arm,
                    row={
                        "ok": bool(row.get("protocol_ok") and not row_has_terminal_failure(row)),
                        "stop_reason": str(row.get("stop_reason") or row.get("error") or ""),
                        "turns": int(row.get("send_count") or 0),
                    },
                )
            store.upsert(row, complete=False)
            print(json.dumps(row, ensure_ascii=False), flush=True)
        finished_at = timestamp()
        store.write(complete=True, extra={"finished_at": finished_at})
        if journal is not None:
            journal.record_run_complete(
                rows=len(store.rows),
                status="done" if store.payload.get("ok") else "failed",
            )
        _write_manifest(
            out,
            provider_id=provider_id,
            arms=(arm,),
            cases=cases,
            max_turns=max_turns,
            transcript_mode=transcript_mode,
            payload=store.payload,
            stop_reason="done" if store.payload.get("ok") else "failed",
            journal=journal,
            layout=layout,
        )
        run_finished = True
        print(json.dumps(store.payload["summary"], ensure_ascii=False, indent=2))
        print(f"report written: {out}")
        return store.payload
    finally:
        if not run_finished and journal is not None:
            try:
                journal.record_run_complete(rows=len(store.rows), status="failed")
            except Exception:
                pass
        if raw_provider is not None:
            try:
                raw_provider.close()
            except Exception:
                pass
        if journal is not None:
            journal.close()


def _self_test() -> None:
    class FakeProvider:
        name = "fake-reviewer"
        location = "fake://reviewer"

        def __init__(self) -> None:
            self.sent: list[str] = []

        def new_chat(self) -> None:
            pass

        def send(self, text: str) -> str:
            self.sent.append(text)
            return json.dumps(
                {
                    "verdict": "changes_requested",
                    "summary": "Ask for the mapped test before approval.",
                    "findings": [
                        {
                            "path": "src/auth.py",
                            "issue": "Verification was requested but no check was observed.",
                            "suggested_fix": "Run tests/test_auth.py or python -m pytest.",
                        }
                    ],
                }
            )

    baseline_prompt = _prompt_for(AUTH_REVIEW_CASE, "baseline")
    current_prompt = _prompt_for(AUTH_REVIEW_CASE, "current")
    assert "tests/test_auth.py" not in baseline_prompt
    assert "tests/test_auth.py" in current_prompt
    fake = TracingProvider(FakeProvider(), case=AUTH_REVIEW_CASE.name, arm="current")
    row = _run_case(fake, AUTH_REVIEW_CASE, "current")
    assert row["protocol_ok"] is True
    assert row["mentions_existing_test"] is True
    assert row["finding_count"] == 1
    print("self-test passed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="A/B for review Verification Map evidence.")
    parser.add_argument("--provider", choices=provider_ids(), default="deepseek")
    parser.add_argument("--arm", choices=ARMS, default="current")
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--transcript-mode", choices=("off", "digest-only", "archive"), default="digest-only")
    parser.add_argument("--rerun-failed", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        _self_test()
        return 0
    payload = run_live(
        str(args.provider),
        str(args.arm),
        int(args.port),
        output=args.output,
        transcript_mode=str(args.transcript_mode),
        rerun_failed=bool(args.rerun_failed),
    )
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
