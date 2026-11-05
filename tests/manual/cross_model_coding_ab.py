"""Cross-model coding audit: local KoboldCpp Writer, Cline advisor/Reviewer.

This is an evidence harness only.  It exercises the production consensus,
project-audit and Review services while keeping the local model as the Writer.
Online Cline models are used only for read-only advice or Review.  No default
production policy is changed by this module.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
import traceback
from pathlib import Path
from types import SimpleNamespace
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from codey.agents.consensus import run_consensus
from codey.app.review_service import run_review_attempt
from codey.completion.verification_map import render_verification_map
from codey.operations.task_context import ProjectTaskContextBuilder
from codey.policies.task_policy import TaskPolicy
from codey.providers import controls
from codey.providers.cline.connection import capture_selection as capture_cline_selection
from codey.providers.cline.connection import open_selection as open_cline_selection
from codey.providers.registry import connect_provider
from codey.reviews.core import render_writer_followup, review_result_payload
from codey.reviews.impact_map import safe_review_impact_map
from codey.runtime.core.run_result import RunResult
from codey.workspace.changes import ChangeTracker
from codey.workspace.task_changes import collect_task_changes
from tests.manual.coding_current_context_ab import CountingProvider, _changed_files
from tests.manual.coding_pipeline_cases import BY_NAME, CASES, CodingCase
from tests.manual.coding_pipeline_value_ab import grade, save, write_files, writer
from tests.support.kernel_runtime import run_project_audit_advisor


def _provider(model_id: str, *, timeout: float = 240) -> CountingProvider:
    if model_id == "local":
        raw = connect_provider("local")
    else:
        raw = open_cline_selection(capture_cline_selection({"model": model_id}))
    wrapped = CountingProvider(raw, timeout=timeout, new_chat_timeout=60)
    wrapped.id = model_id
    wrapped.model_id = model_id
    return wrapped


def _advisor_call(model_id: str, prompt: str) -> dict[str, Any]:
    advisor = _provider(model_id, timeout=180)
    started = time.monotonic()
    try:
        advisor.new_chat()
        reply = advisor.send(prompt)
        return {
            "model": model_id,
            "reply": reply,
            "reply_chars": len(reply),
            "prompt_chars": sum(len(item) for item in advisor.prompts),
            "requests": len(advisor.prompts),
            "seconds": round(time.monotonic() - started, 3),
        }
    finally:
        advisor.close()


def _stage_prompt(stage: str, case: CodingCase, root: Path, context: str) -> str:
    action = "implementation plan" if stage == "plan" else "read-only project audit"
    files = "\n\n".join(f"--- {path} ---\n{content}" for path, content in case.files.items())
    return (
        "You are an independent coding advisor. Provide a bounded " + action + ".\n"
        "Do not edit files, run commands, or claim that tests passed. Identify concrete files, "
        "risks, and one verification command. Your notes will be given privately to a local Writer.\n\n"
        f"Task:\n{case.task}\n\nProject root: {root}\n\nProject context:\n{context}\n\n"
        f"Current files:\n{files}"
    )


def _quality(root: Path, case: CodingCase) -> dict[str, Any]:
    result = grade(root, case)
    return {
        "quality_passed": result["quality_passed"],
        "quality_score": result["quality_score"],
        "product_behavior_passed": result["product_behavior_passed"],
        "visible_suite": result["visible_suite"],
        "changed_files": result["changed_files"],
        "scope_ok": result["scope_ok"],
    }


def _inject_known_tag_defect(root: Path, tracker: ChangeTracker) -> None:
    """Create a controlled reviewer holdout after Writer has finished.

    This is deliberately marked as seeded evidence: it tests whether an
    independent Reviewer can find and repair a known, reproducible defect.
    """
    path = root / "tags.py"
    text = path.read_text(encoding="utf-8")
    marker = "def parse_tags"
    if marker not in text:
        raise AssertionError("tag-feature Writer output has no parse_tags function")
    updated = text.split(marker, 1)[0] + (
        "def parse_tags(text):\n"
        "    return sorted(tag.lower() for tag in {tag.strip() for tag in text.split(',') if tag.strip()})\n"
    )
    path.write_text(updated, encoding="utf-8")
    tracker.capture_after("tags.py")


def _writer_run(local: CountingProvider, case: CodingCase, root: Path, state: Path,
                tracker: ChangeTracker, task: str, *, max_turns: int = 24) -> dict[str, Any]:
    result, events = writer(local, case, root, state, task=task, max_turns=max_turns,
                            tracker=tracker, events_out=[])
    return {"result": result, "event_count": len(events), **_quality(root, case)}


def _review_once(reviewer_model: str, root: Path, state: Path, case: CodingCase,
                 changes: dict[str, Any], writer_result: RunResult) -> dict[str, Any]:
    reviewer = _provider(reviewer_model, timeout=240)
    events: list[dict[str, Any]] = []
    ctx = SimpleNamespace(emit=events.append, state_home=state)
    project_map = ProjectTaskContextBuilder().build(
        project=root, task=case.task, session_id="cross-model-review", run_id="review-map",
        continue_task=False, provider_session_changed=False,
    ).project_map
    try:
        reviewed = run_review_attempt(
            ctx,
            session_id="cross-model-review",
            project=str(root),
            task=case.task,
            writer_summary=writer_result.summary,
            changes=changes,
            recent_log="Cross-model coding audit Writer result.",
            change_brief="",
            project_map=project_map,
            verification_map=render_verification_map(root, changes, project_map=project_map),
            review_impact_map=safe_review_impact_map(root, changes),
            execution_evidence="",
            reviewer_id=f"cline:{reviewer_model}",
            reviewer=reviewer,
            self_review=False,
            run_id=f"cross-model-review-{time.time_ns()}",
        )
        review = reviewed[1]
        return {
            "model": reviewer_model,
            "result": review_result_payload(review),
            "_review_object": review,
            "needs_writer_repair": bool(getattr(review, "needs_writer_repair", False)),
            "approved": bool(getattr(review, "approved", False)),
            "prompt_chars": sum(len(item) for item in reviewer.prompts),
            "requests": len(reviewer.prompts),
            "raw_replies": reviewer.replies,
            "events": events,
        }
    except Exception as exc:
        return {
            "model": reviewer_model,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(limit=12),
            "prompt_chars": sum(len(item) for item in reviewer.prompts),
            "requests": len(reviewer.prompts),
            "raw_replies": reviewer.replies,
            "events": events,
        }


def _run_case(local: CountingProvider, case: CodingCase, arm: str, *,
              advisor_model: str, second_advisor_model: str, reviewer_model: str,
              max_turns: int) -> dict[str, Any]:
    started = time.monotonic()
    local_prompt_start = len(local.prompts)
    row: dict[str, Any] = {"case": case.name, "arm": arm, "writer_model": "local"}
    with tempfile.TemporaryDirectory() as temporary:
        root, state = Path(temporary) / "project", Path(temporary) / "state"
        root.mkdir()
        write_files(root, case.files)
        tracker = ChangeTracker(root)
        context = ProjectTaskContextBuilder().build(
            project=root, task=case.task, session_id="cross-model-ab", run_id="prepare",
            continue_task=False, provider_session_changed=False,
        )
        task = case.task
        try:
            if arm == "plan":
                note = _advisor_call(advisor_model, _stage_prompt("plan", case, root, context.project_map))
                row["advisor"] = note
                task += f"\n\nPrivate independent plan (verify against files):\n{note['reply'][:6000]}"
            elif arm == "audit":
                if not case.files:
                    raise ValueError("pre-audit requires an existing project")
                advisor = _provider(advisor_model, timeout=240)
                try:
                    advisor.new_chat()
                    before = _changed_files(root, case.files)
                    audit = run_project_audit_advisor(
                        advisor, root, case.task,
                        parent_policy=TaskPolicy(grants=frozenset({
                            "control", "project.read", "project.write", "project.verify",
                        })),
                        context=context.project_map,
                        advisor_id=f"cline:{advisor_model}",
                    )
                    row["advisor"] = {"model": advisor_model, "reply": audit,
                                      "reply_chars": len(audit), "prompt_chars": sum(len(x) for x in advisor.prompts),
                                      "requests": len(advisor.prompts)}
                    if _changed_files(root, case.files) != before:
                        raise AssertionError("independent audit modified files")
                    if audit:
                        task += f"\n\nPrivate independent audit (verify against files):\n{audit[:6000]}"
                finally:
                    advisor.close()
            elif arm == "consensus":
                if case.files:
                    raise ValueError("consensus plan applies to a new project")
                local.new_chat()
                consensus_local_start = len(local.prompts)
                advisors = {advisor_model, second_advisor_model}
                providers: dict[str, CountingProvider] = {}

                def connect(model_id: str) -> CountingProvider:
                    provider = _provider(model_id, timeout=180)
                    providers[model_id] = provider
                    return provider

                try:
                    result = run_consensus(
                        selected_provider=local,
                        selected_provider_id="local",
                        task=case.task,
                        provider_ids=("local", *sorted(advisors)),
                        provider_labels={model: model for model in advisors},
                        availability=lambda: {model: True for model in advisors},
                        connect_existing=connect,
                        context=context.project_map,
                        plan=True,
                        draft_first=True,
                        max_advisors=2,
                    )
                    if result is None:
                        raise RuntimeError("cross-model consensus returned no result")
                    row["consensus"] = {
                        "advisor_count": result.advisor_count,
                        "degraded": result.degraded,
                        "degraded_reasons": list(result.degraded_reasons),
                        "answer_chars": len(result.answer),
                        "local_requests": len(local.prompts) - consensus_local_start,
                        "advisor_requests": {model: len(provider.prompts) for model, provider in providers.items()},
                    }
                    task += f"\n\nPrivate cross-model consensus plan (verify against files):\n{result.answer[:8000]}"
                finally:
                    for provider in providers.values():
                        provider.close()

            initial = _writer_run(local, case, root, state, tracker, task, max_turns=max_turns)
            writer_result = initial["result"]
            row.update({key: value for key, value in initial.items() if key != "result"})
            row["writer_stop_reason"] = writer_result.stop_reason
            row["writer_turns"] = writer_result.turns
            row["writer_summary"] = writer_result.summary
            if arm in {"seeded_baseline", "review_seeded"}:
                if case.name != "tag-feature" or writer_result.stop_reason != "done":
                    raise ValueError("seeded reviewer control requires a completed tag-feature Writer")
                _inject_known_tag_defect(root, tracker)
                row["seeded_defect"] = "case-sensitive duplicate removal in parse_tags"
                row["final_before_review"] = _quality(root, case)
            if arm in {"review", "review_seeded"} and writer_result.stop_reason == "done" and row["changed_files"]:
                changes = collect_task_changes(root, tracker)
                review = _review_once(reviewer_model, root, state, case, changes, writer_result)
                review_object = review.pop("_review_object", None)
                row["review"] = review
                if review.get("needs_writer_repair"):
                    if review_object is None:
                        raise RuntimeError("review requested repair without a ReviewResult")
                    followup = render_writer_followup(case.task, review_object, change_brief="")
                    repaired = _writer_run(local, case, root, state / "repair", tracker, followup, max_turns=max_turns)
                    repaired_result = repaired["result"]
                    row["repair"] = {key: value for key, value in repaired.items() if key != "result"}
                    row["repair"]["stop_reason"] = repaired_result.stop_reason
                    row["repair"]["turns"] = repaired_result.turns
                    if repaired_result.stop_reason == "done":
                        post_review = _review_once(
                            reviewer_model, root, state, case,
                            collect_task_changes(root, tracker), repaired_result,
                        )
                        post_review.pop("_review_object", None)
                        row["post_review"] = post_review
            row["final"] = _quality(root, case)
            row["success"] = bool(
                row["writer_stop_reason"] == "done"
                and row["final"]["quality_passed"]
                and row["final"]["scope_ok"]
            )
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
            row["traceback"] = traceback.format_exc(limit=16)
            row["final"] = _quality(root, case)
            row["success"] = False
    row["writer_prompt_chars"] = sum(len(item) for item in local.prompts[local_prompt_start:])
    row["writer_requests"] = len(local.prompts) - local_prompt_start
    row["seconds"] = round(time.monotonic() - started, 3)
    return row


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", action="append", choices=tuple(BY_NAME))
    parser.add_argument("--arm", action="append", choices=(
        "baseline", "plan", "audit", "consensus", "review", "seeded_baseline", "review_seeded",
    ))
    parser.add_argument("--advisor-model", default="cline-free/nemotron-3.5-super-vl")
    parser.add_argument("--second-advisor-model", default="nvidia/nemotron-3.5-lightning-free")
    parser.add_argument("--reviewer-model", default="reflection/beam-free")
    parser.add_argument("--max-turns", type=int, default=24)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    cases = tuple(BY_NAME[name] for name in args.case) if args.case else CASES
    arms = tuple(args.arm) if args.arm else ("baseline", "plan", "audit", "consensus", "review")
    controls.begin_task_context("cross-model-coding-ab")
    local = _provider("local", timeout=300)
    payload: dict[str, Any] = {
        "writer_model": "local",
        "advisor_model": args.advisor_model,
        "second_advisor_model": args.second_advisor_model,
        "reviewer_model": args.reviewer_model,
        "cases": [case.name for case in cases],
        "arms": list(arms),
        "rows": [],
    }
    try:
        for case in cases:
            for arm in arms:
                if arm == "audit" and not case.files:
                    continue
                if arm == "consensus" and case.files:
                    continue
                row = _run_case(local, case, arm,
                                advisor_model=args.advisor_model,
                                second_advisor_model=args.second_advisor_model,
                                reviewer_model=args.reviewer_model,
                                max_turns=args.max_turns)
                payload["rows"].append(row)
                save(args.output, payload)
                print(json.dumps({key: row.get(key) for key in (
                    "case", "arm", "success", "writer_stop_reason", "writer_turns",
                    "writer_requests", "writer_prompt_chars", "seconds", "error",
                )}, ensure_ascii=False), flush=True)
    finally:
        local.close()
        controls.end_task_context()
    summary: dict[str, Any] = {}
    for arm in arms:
        rows = [row for row in payload["rows"] if row.get("arm") == arm]
        summary[arm] = {
            "count": len(rows),
            "success": sum(bool(row.get("success")) for row in rows),
            "errors": sum("error" in row for row in rows),
            "review_repairs": sum(bool(row.get("review", {}).get("needs_writer_repair")) for row in rows),
        }
    payload["summary"] = summary
    save(args.output, payload)
    return 0 if payload["rows"] and all(row.get("success") for row in payload["rows"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
