"""Final-quality Review ablations from identical seeded candidate diffs.

Seeds are experimental setup, not claimed Writer output. Reviews, one repair,
and the post-repair snapshot review use production services/coordinator.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from codey.app.review_service import run_review_attempt
from codey.completion.verification_map import render_verification_map
from codey.operations.task_context import ProjectTaskContextBuilder
from codey.providers import controls
from codey.providers.registry import connect_provider
from codey.reviews.coordinator import ReviewCoordinator
from codey.reviews.core import review_result_payload
from codey.reviews.impact_map import safe_review_impact_map
from codey.runtime.core.run_result import RunResult
from codey.workspace.changes import ChangeTracker
from codey.workspace.task_changes import collect_task_changes
from tests.manual.coding_pipeline_cases import BY_NAME
from tests.manual.coding_pipeline_value_ab import (
    EvidenceProvider,
    event_rows,
    grade,
    save,
    source_metadata,
    write_files,
    writer,
)

ARMS = ("none", "review", "repair")


def seed_files(name, correct):
    if name == "discount-bug":
        validation = "    if percent < 0 or percent > 100:\n        raise ValueError('percent')\n" if correct else ""
        return {"pricing.py": "def discounted_price(price, percent):\n" + validation
                + "    return price * (1 - percent / 100)\n"}
    implementation = ("    return sorted({tag.strip().lower() for tag in text.split(',') if tag.strip()})\n"
                      if correct else "    return sorted(tag.lower() for tag in {t.strip() for t in text.split(',') if t.strip()})\n")
    return {"tags.py": "def render_title(value):\n    return value.title()\n\ndef parse_tags(text):\n" + implementation}


def run_arm(provider, case, correct, arm):
    mark, started = provider.mark(), time.monotonic()
    row = {"case": case.name, "seed": "correct" if correct else "flawed", "arm": arm,
           "reviews": [], "repairs": [], "events": []}
    with tempfile.TemporaryDirectory() as temporary:
        root, state = Path(temporary) / "project", Path(temporary) / "state"
        root.mkdir()
        write_files(root, case.files)
        tracker = ChangeTracker(root)
        for rel, content in seed_files(case.name, correct).items():
            tracker.capture_before(rel)
            write_files(root, {rel: content})
            tracker.capture_after(rel)
        row["initial_quality_score"] = grade(root, case)["quality_score"]
        check = subprocess.run([sys.executable, "-B", "-m", "pytest", "-q"], cwd=root,
                               capture_output=True, text=True, timeout=30)
        row["visible_verification"] = {"exit_code": check.returncode, "output": check.stdout + check.stderr,
                                       "provenance": "harness subprocess; not a kernel execution receipt"}
        result = RunResult("Seeded candidate for controlled review experiment.", "done", 0, check.returncode == 0, True, True)
        try:
            if arm != "none":
                ctx = SimpleNamespace(emit=row["events"].append, state_home=state)
                def project_map():
                    return ProjectTaskContextBuilder().build(project=root, task=case.task,
                        session_id="review-ab", run_id="map", continue_task=False,
                        provider_session_changed=False).project_map

                def review(**kwargs):
                    kwargs.pop("writer_id")
                    outcome = run_review_attempt(ctx, **kwargs,
                        review_impact_map=safe_review_impact_map(root, kwargs["changes"]),
                        reviewer_id="local", reviewer=provider, self_review=True,
                        run_id=f"review-ab-{time.time_ns()}")
                    row["reviews"].append(review_result_payload(outcome[1]))
                    return outcome

                def repair(task, checkpoint):
                    repair_events = []
                    repaired, _events = writer(provider, case, root, state / "revisions", task=task,
                        project_map=project_map(), context=True, tracker=tracker,
                        changed=checkpoint.changed_files, events_out=repair_events)
                    row["repairs"].append({"stop_reason": repaired.stop_reason, "turns": repaired.turns,
                                           "summary": repaired.summary, "events": event_rows(repair_events)})
                    return repaired

                coordinator = ReviewCoordinator(collect_task_changes)
                def cycle(candidate, *, allow_repair):
                    return coordinator.run_cycle(project=root, tracker=tracker, session_id="review-ab",
                        task=case.task, result=candidate, task_changed=True,
                        changes=collect_task_changes(root, tracker), changes_dirty=False, writer_id="local",
                        recent_log="Experimental seed; visible verification output:\n" + check.stdout + check.stderr,
                        render_change_brief=lambda: "", execution_evidence="", successful_checks=(),
                        checkpoint_prompt="", checks_before_review_followup=False, stop_requested=lambda: False,
                        refresh_project_map=project_map,
                        build_verification_map=lambda changes, map_text: render_verification_map(root, changes, project_map=map_text),
                        run_review=review, close_writer_for_review=lambda: None, repair_writer=repair,
                        set_checkpoint_status=lambda status: row["events"].append({"checkpoint": status}),
                        emit_review_unavailable=lambda: row["events"].append({"review_unavailable": True}),
                        allow_repair=allow_repair)
                outcome = cycle(result, allow_repair=arm == "repair")
                if outcome.review_repair_attempted and outcome.result.stop_reason == "done":
                    outcome = cycle(outcome.result, allow_repair=False)
                result = outcome.result
                row["approved_current_snapshot"] = outcome.approved_current_snapshot
            row["stop_reason"] = result.stop_reason
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
        row.update(grade(root, case))
    row["seconds"] = round(time.monotonic() - started, 3)
    row["success"] = (row.get("stop_reason") == "done" and row["quality_passed"] and row["scope_ok"]
                      and (arm == "none" or row.get("approved_current_snapshot", False)))
    row.update(provider.evidence(mark))
    return row


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reverse", action="store_true")
    args = parser.parse_args()
    controls.begin_task_context("coding-review-value-ab")
    provider = EvidenceProvider(connect_provider("local"))
    payload = {"model": provider.model, "seeded_candidates": True, "rows": [], **source_metadata()}
    try:
        for name in ("discount-bug", "tag-feature"):
            for correct in (False, True):
                for arm in reversed(ARMS) if args.reverse else ARMS:
                    row = run_arm(provider, BY_NAME[name], correct, arm)
                    payload["rows"].append(row)
                    save(args.output, payload)
                    print(json.dumps({key: row.get(key) for key in ("case", "seed", "arm", "success",
                        "initial_quality_score", "quality_score", "stop_reason", "approved_current_snapshot",
                        "http_requests", "input_context_tokens", "seconds", "error")}), flush=True)
    finally:
        provider.close()
        controls.end_task_context()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
