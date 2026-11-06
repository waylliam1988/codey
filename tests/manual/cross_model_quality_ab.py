"""Quality-first live ablation with a shared independent Review/repair endpoint.

No user settings are changed. Read-only online models advise/review a local
Writer in isolated projects. Workflow stops and artifact quality are separate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import subprocess
import sys
import tempfile
import time
import traceback
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from codey.agents.consensus import run_consensus
from codey.app.review_service import run_review_attempt
from codey.completion.verification_map import render_verification_map
from codey.operations.task_context import ProjectTaskContextBuilder
from codey.policies.task_policy import TaskPolicy
from codey.providers import controls
from codey.providers.cline.connection import capture_selection, open_selection
from codey.providers.local_connection import capture_selection as capture_local_selection
from codey.providers.local_connection import open_selection as open_local_selection
from codey.reviews.core import render_writer_followup, review_result_payload
from codey.reviews.impact_map import safe_review_impact_map
from codey.workspace.change_brief import ChangeBrief, new_project_change_brief, project_audit_change_brief
from codey.workspace.changes import ChangeTracker
from codey.workspace.task_changes import collect_task_changes
from tests.manual.coding_pipeline_value_ab import EvidenceProvider, event_rows, grade, save, write_files, writer
from tests.manual.cross_model_quality_cases import FIXTURE_BY_NAME, FIXTURES
from tests.support.kernel_runtime import run_project_audit_advisor

ARMS = ("baseline", "plan", "audit", "consensus_one", "consensus_two")
ROOT = Path(__file__).resolve().parents[2]
LIGHTNING = "nvidia/nemotron-3.5-lightning-free"


def online_selection(model):
    # Explicit, experiment-local control supported by this model's catalog.
    # Its default thinking mode exhausted the output budget in contract preflight.
    return {"model": model, **({"thinking": False} if model == LIGHTNING else {})}


def source_hashes():
    paths = [*ROOT.joinpath("codey").rglob("*.py"),
             Path(__file__), Path(__file__).with_name("cross_model_quality_cases.py"),
             Path(__file__).with_name("coding_pipeline_value_ab.py")]
    return {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(paths)}


def applicable(case, arm):
    return not ((arm == "audit" and not case.files) or (arm.startswith("consensus_") and case.files))


def schedule(cases, arms, repeats, order_seed):
    rows = []
    for repeat in range(repeats):
        ordered_cases = list(cases)
        random.Random(order_seed + repeat).shuffle(ordered_cases)
        for case in ordered_cases:
            ordered_arms = [arm for arm in arms if applicable(case, arm)]
            random.Random(f"{order_seed}:{case.name}:{repeat // 2}").shuffle(ordered_arms)
            if repeat % 2:
                ordered_arms.reverse()
            rows.extend((repeat, case.name, arm) for arm in ordered_arms)
    return rows


def artifact_passed(quality):
    return bool(quality["quality_passed"] and quality["scope_ok"])


def repair_budget(maximum, used):
    return min(12, max(0, maximum - used))


def summarize(rows):
    summary = {}
    for arm in ARMS:
        selected = [row for row in rows if row["arm"] == arm]
        if selected:
            summary[arm] = {
                "count": len(selected),
                "artifact_quality_passed": sum(row["artifact_quality_passed"] for row in selected),
                "writer_complete": sum(row.get("final_stop_reason") == "done" for row in selected),
                "review_approved": sum(row.get("review_approved", False) for row in selected),
                "review_repairs": sum("repair" in row for row in selected),
                "execution_errors": sum("error" in row for row in selected),
                "treatment_delivered": sum(row.get("treatment_delivered", False) for row in selected),
            }
    return summary


def provider(model, records, purpose, *, local_output_tokens=4096):
    if model == "local":
        selection = capture_local_selection()
        raw = open_local_selection(replace(selection, output_tokens=min(
            local_output_tokens, selection.output_tokens or selection.context_reserve_tokens,
        )))
    else:
        raw = open_selection(capture_selection(online_selection(model)))
    observed = EvidenceProvider(raw, timeout=300 if model == "local" else 180)
    observed.id = model
    observed.model_id = raw.model
    records.append((purpose, observed, observed.mark()))
    return observed


def plan_brief(case, note):
    if not case.files:
        return new_project_change_brief(case.task, note)
    # Production auto-planning applies to greenfield projects. Existing-project
    # planning is an explicitly labelled diagnostic of the previous failure.
    return ChangeBrief(source="existing-project planning experiment", user_intent=case.task,
                       advisor_notes=(note,), risks=("Verify advice against actual files.",))


def prepare(local, case, root, context, arm, config, records, row):
    if arm == "baseline":
        row["treatment_delivered"] = True
        return None
    if arm == "plan":
        advisor = provider(config.advisor, records, "plan")
        advisor.new_chat()
        files = "\n\n".join(f"--- {name} ---\n{text}" for name, text in case.files.items())
        note = advisor.send(
            "Provide a concise independent implementation plan. You are read-only; do not call tools, "
            "edit files or claim tests ran. Identify edge cases and concrete verification.\n\n"
            f"Task:\n{case.task}\n\nProject map:\n{context}\n\nExisting files:\n{files}"
        )
        row["stage_output"] = note
        row["production_stage_applicable"] = not bool(case.files)
        row["treatment_delivered"] = bool(note.strip())
        return plan_brief(case, note) if note.strip() else None
    if arm == "audit":
        advisor = provider(config.advisor, records, "audit")
        advisor.new_chat()
        before = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}
        note = run_project_audit_advisor(
            advisor, root, case.task,
            parent_policy=TaskPolicy(grants=frozenset({"control", "project.read", "project.write", "project.verify"})),
            context=context, advisor_id=f"cline:{config.advisor}",
            max_turns=config.audit_turns,
        )
        after = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}
        if before != after:
            raise AssertionError("read-only advisor changed project files")
        row["stage_output"] = note
        row["treatment_delivered"] = bool(note.strip())
        return project_audit_change_brief(case.task, (note,)) if note.strip() else None
    ids = (config.advisor,) if arm == "consensus_one" else (config.advisor, config.second_advisor)
    if len(set(ids)) != len(ids):
        raise ValueError("consensus requires distinct advisor models")
    local.new_chat()
    result = run_consensus(
        selected_provider=local, selected_provider_id="local", task=case.task,
        provider_ids=("local", *ids), provider_labels={model: model for model in ids},
        availability=lambda: dict.fromkeys(ids, True),
        connect_existing=lambda model: provider(model, records, "consensus_advisor"),
        context=context, plan=True, draft_first=True, max_advisors=len(ids),
    )
    if result is None:
        raise RuntimeError("consensus returned no result")
    row["consensus"] = {"advisor_count": result.advisor_count, "degraded": result.degraded,
                        "degraded_reasons": list(result.degraded_reasons)}
    row["stage_output"] = result.answer
    row["treatment_delivered"] = result.advisor_count == len(ids) and not result.degraded
    return new_project_change_brief(case.task, result.answer)


def run_writer(local, case, root, state, tracker, task, project_map, budget):
    result, events = writer(local, case, root, state, tracker=tracker, task=task,
                            project_map=project_map, context=True, max_turns=budget)
    return result, {"stop_reason": result.stop_reason, "turns": result.turns,
                    "summary": result.summary, "events": event_rows(events), "quality": grade(root, case)}


def review(case, root, state, tracker, result, brief, config, records, log):
    reviewer = provider(config.reviewer, records, "review")
    events = []
    changes = collect_task_changes(root, tracker)
    project_map = ProjectTaskContextBuilder().build(
        project=root, task=case.task, session_id="quality-ab", run_id="review-map",
        continue_task=False, provider_session_changed=False,
    ).project_map
    reviewed = run_review_attempt(
        SimpleNamespace(emit=events.append, state_home=state), session_id="quality-ab",
        project=str(root), task=case.task, writer_summary=result.summary, changes=changes,
        recent_log=json.dumps(log["events"], ensure_ascii=False)[-12000:],
        change_brief=brief.render(audience="reviewer") if brief else "", project_map=project_map,
        verification_map=render_verification_map(root, changes, project_map=project_map),
        review_impact_map=safe_review_impact_map(root, changes), execution_evidence="",
        reviewer_id=f"cline:{config.reviewer}", reviewer=reviewer,
        self_review=False, run_id=f"quality-review-{time.time_ns()}",
    )[1]
    return reviewed, {"result": review_result_payload(reviewed), "events": events,
                      "approved": reviewed.approved, "needs_writer_repair": reviewed.needs_writer_repair,
                      "self_review": reviewed.identity.self_review, "model_id": reviewed.identity.model_id}


def run_case(case, arm, repeat, config):
    started = time.monotonic()
    records = []
    row = {"case": case.name, "arm": arm, "repeat": repeat,
           "treatment_delivered": False, "production_stage_applicable": True}
    phase = "provider_setup"
    with tempfile.TemporaryDirectory(prefix="codey-quality-") as directory:
        root, state = Path(directory) / "project", Path(directory) / "state"
        root.mkdir()
        write_files(root, case.files)
        tracker = ChangeTracker(root)
        try:
            local = provider("local", records, "writer", local_output_tokens=config.local_output_tokens)
            row["writer_model"] = local.model
            project_map = ProjectTaskContextBuilder().build(
                project=root, task=case.task, session_id="quality-ab", run_id="prepare",
                continue_task=False, provider_session_changed=False,
            ).project_map
            phase = "advice"
            brief = prepare(local, case, root, project_map, arm, config, records, row)
            task = brief.apply_to_task(case.task) if brief else case.task
            phase = "writer"
            result, initial = run_writer(local, case, root, state, tracker, task, project_map, config.max_turns)
            row["initial"] = initial
            row["final_stop_reason"] = result.stop_reason
            if result.stop_reason == "done" and initial["quality"]["changed_files"]:
                phase = "review"
                reviewed, row["review"] = review(
                    case, root, state, tracker, result, brief, config, records, initial,
                )
                row["review_approved"] = reviewed.approved
                remaining = repair_budget(config.max_turns, result.turns)
                if reviewed.needs_writer_repair and remaining:
                    phase = "repair"
                    followup = render_writer_followup(
                        case.task, reviewed, change_brief=brief.render() if brief else "",
                    )
                    repaired, repair = run_writer(local, case, root, state / "repair", tracker,
                                                  followup, project_map, remaining)
                    row["repair"] = repair
                    row["final_stop_reason"] = repaired.stop_reason
                    if repaired.stop_reason == "done":
                        phase = "post_review"
                        _, row["post_review"] = review(
                            case, root, state, tracker, repaired, brief, config, records, repair,
                        )
                        row["review_approved"] = row["post_review"]["approved"]
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
            row["failure_stage"] = phase
            row["traceback"] = traceback.format_exc(limit=15)
        finally:
            row["final"] = grade(root, case)
            row["artifact_quality_passed"] = artifact_passed(row["final"])
            row["providers"] = []
            for purpose, observed, mark in records:
                row["providers"].append({"purpose": purpose, "model": observed.model,
                                         **observed.evidence(mark)})
                observed.close()
    row["seconds"] = round(time.monotonic() - started, 3)
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", action="append", choices=tuple(FIXTURE_BY_NAME))
    parser.add_argument("--arm", action="append", choices=ARMS)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--order-seed", type=int, default=61106)
    parser.add_argument("--max-turns", type=int, default=32)
    parser.add_argument("--audit-turns", type=int, default=8)
    parser.add_argument("--local-output-tokens", type=int, default=4096)
    parser.add_argument("--advisor", default="nvidia/nemotron-3.5-lightning-free")
    parser.add_argument("--second-advisor", default="cline-free/solar-mini4")
    parser.add_argument("--reviewer", default="cline-free/solar-mini4")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if args.local_output_tokens <= 0:
        parser.error("--local-output-tokens must be positive")
    cases = tuple(FIXTURE_BY_NAME[name].case for name in args.case) if args.case else tuple(f.case for f in FIXTURES)
    arms = args.arm or ARMS
    planned = schedule(cases, arms, args.repeats, args.order_seed)
    metadata = {"advisor": args.advisor, "second_advisor": args.second_advisor, "reviewer": args.reviewer,
                "max_turns": args.max_turns, "audit_turns": args.audit_turns,
                "local_output_tokens": args.local_output_tokens, "local_request_timeout_seconds": 300,
                "schedule": planned, "order_seed": args.order_seed,
                "model_seed_controlled": False, "map_and_current_context": True,
                "online_selections": {model: online_selection(model) for model in (
                    args.advisor, args.second_advisor, args.reviewer)},
                "total_writer_turn_budget": True, "review_repair_limit": 1, "review_fix_turns": 12,
                "source_sha256": source_hashes(),
                "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()}
    if args.resume:
        payload = json.loads(args.output.read_text(encoding="utf-8"))
        # A resume must never silently mix model, code, budget or order changes.
        if json.loads(json.dumps(metadata)) != payload["metadata"]:
            raise ValueError("resume configuration/source mismatch")
    else:
        if args.output.exists():
            raise FileExistsError(args.output)
        payload = {"metadata": metadata, "rows": []}
    complete = {(r["repeat"], r["case"], r["arm"]) for r in payload["rows"]}
    save(args.output, payload)
    controls.begin_task_context("cross-model-quality-ab")
    try:
        for repeat, name, arm in planned:
            if (repeat, name, arm) in complete:
                continue
            if args.output.with_suffix(".stop").exists():
                payload["interruption"] = "stop sentinel; incomplete matrix is not success"
                break
            print(json.dumps({"starting": [repeat, name, arm]}, ensure_ascii=False), flush=True)
            row = run_case(FIXTURE_BY_NAME[name].case, arm, repeat, args)
            payload["rows"].append(row)
            payload["summary"] = summarize(payload["rows"])
            save(args.output, payload)
            print(json.dumps({key: row.get(key) for key in (
                "repeat", "case", "arm", "artifact_quality_passed", "final_stop_reason",
                "review_approved", "treatment_delivered", "seconds", "error",
            )}, ensure_ascii=False), flush=True)
    finally:
        controls.end_task_context()
        payload["source_unchanged"] = source_hashes() == metadata["source_sha256"]
        save(args.output, payload)
    return 0 if len(payload["rows"]) == len(planned) and payload["rows"] and all(
        row["artifact_quality_passed"] for row in payload["rows"]
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
