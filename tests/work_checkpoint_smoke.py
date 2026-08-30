"""Manual live smoke for interrupted execution checkpoint recovery.

This is intentionally outside pytest collection. It uses real web providers and
is run explicitly when validating provider/session recovery.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codey.agents.runner import run
from codey.workspace.changes import ChangeTracker, collect_changes
from codey.runtime.events import RunEvent, render_run_event
from codey.providers.registry import connect_provider, provider_ids
from codey.reviews.core import (
    parse_review_with_repair,
    render_review_prompt,
    render_writer_followup,
)
from codey.completion.verification_map import render_verification_map
from codey.runs.work_checkpoint import WorkCheckpointStore, render_work_checkpoint


def run_smoke(writer_id: str, reviewer_id: str, port: int) -> dict:
    base = Path(tempfile.mkdtemp(prefix="codey-checkpoint-live-")).resolve()
    root = base / "project"
    root.mkdir()
    state_home = base / "state"
    store = WorkCheckpointStore(state_home)
    session_id = "live-checkpoint"
    tracker = ChangeTracker(root)
    checkpoint = store.start(
        run_id="interrupted-run",
        session_id=session_id,
        project=root,
        task="Create and verify the greeting module",
    )
    events: list[str] = []

    def on_event(event: RunEvent) -> None:
        nonlocal checkpoint
        events.append(render_run_event(event))
        if event.kind != "tool" or event.call is None or event.outcome is None:
            return
        if event.call.name == "edit" and event.outcome.ok and event.outcome.changed:
            checkpoint = store.record_edit(checkpoint, str(event.call.args.get("path") or ""))
        elif event.call.name == "run":
            checkpoint = store.record_run(
                checkpoint,
                command=str(event.call.args.get("command") or ""),
                cwd=str(event.call.args.get("path") or "."),
                ok=event.outcome.ok and event.outcome.exit_code == 0,
                workspace_revision=1,
            )

    try:
        (root / "test_app.py").write_text(
            "import unittest\n\nfrom app import greeting\n\n"
            "class AppTests(unittest.TestCase):\n"
            "    def test_greeting(self):\n"
            "        self.assertEqual(greeting(), 'hello')\n",
            encoding="utf-8",
        )
        writer = connect_provider(writer_id, port=port)
        try:
            first = run(
                writer,
                root,
                "Create app.py with greeting() returning 'hello'. Your first action must be "
                "an edit that creates app.py. After that, run python -m unittest and finish.",
                max_turns=1,
                fresh_chat=True,
                change_tracker=tracker,
                on_event=on_event,
            )
        finally:
            writer.close()
        checkpoint = store.set_status(checkpoint, "interrupted", first.stop_reason)
        if not checkpoint.changed_files or first.stop_reason == "done":
            raise RuntimeError("the controlled first run did not stop after a real edit")

        checkpoint = store.reconcile(store.load(session_id))
        writer = connect_provider(writer_id, port=port)
        try:
            second = run(
                writer,
                root,
                "Continue the unfinished greeting task. Verify the current file and run "
                "python -m unittest before finishing.",
                max_turns=8,
                fresh_chat=True,
                change_tracker=tracker,
                work_checkpoint=render_work_checkpoint(checkpoint),
                on_event=on_event,
            )
        finally:
            writer.close()

        verification = subprocess.run(
            [sys.executable, "-B", "-m", "unittest"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        changes = collect_changes(root, tracker)
        verification_map = render_verification_map(
            root,
            changes,
            checks_after_last_change=checkpoint.successful_checks_after_last_change,
        )
        reviewer = connect_provider(reviewer_id, port=port, open_if_missing=False)
        try:
            review_reply = reviewer.send(render_review_prompt(
                project=str(root),
                task="Create and verify the greeting module",
                writer_summary=second.summary,
                changes=changes,
                recent_log="\n".join(events[-80:]),
                verification_map=verification_map,
            ))
            review = parse_review_with_repair(review_reply, reviewer.send)
        finally:
            reviewer.close()

        followup = None
        if not review.approved:
            checkpoint = store.set_status(checkpoint, "fixing_review", "")
            writer = connect_provider(writer_id, port=port)
            try:
                followup = run(
                    writer,
                    root,
                    render_writer_followup(
                        "Create and verify the greeting module",
                        review,
                    ),
                    max_turns=8,
                    fresh_chat=True,
                    change_tracker=tracker,
                    work_checkpoint=render_work_checkpoint(checkpoint),
                    on_event=on_event,
                )
            finally:
                writer.close()
            verification = subprocess.run(
                [sys.executable, "-B", "-m", "unittest"],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )

        ok = (
            second.stop_reason == "done"
            and (followup is None or followup.stop_reason == "done")
            and verification.returncode == 0
        )
        if ok:
            store.delete(session_id)
        return {
            "ok": ok,
            "first_stop_reason": first.stop_reason,
            "recorded_files": [item.path for item in checkpoint.changed_files],
            "resume_prompt_injected": True,
            "second_stop_reason": second.stop_reason,
            "independent_check": verification.returncode,
            "review_approved": review.approved,
            "review_followup_stop_reason": (
                followup.stop_reason if followup is not None else "not_needed"
            ),
            "review_summary": review.summary,
            "review_findings": [
                {
                    "path": item.path,
                    "issue": item.issue,
                    "suggested_fix": item.suggested_fix,
                }
                for item in review.findings
            ],
            "verification_map": verification_map,
            "checkpoint_deleted": store.load(session_id) is None,
            "events": events,
        }
    finally:
        shutil.rmtree(base, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--writer", choices=provider_ids(), default="deepseek")
    parser.add_argument("--reviewer", choices=provider_ids(), default="glm")
    parser.add_argument("--port", type=int, default=9222)
    args = parser.parse_args()
    if args.writer == args.reviewer:
        parser.error("writer and reviewer must differ")
    result = run_smoke(args.writer, args.reviewer, args.port)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
