"""Online-only contract probes; controlled candidates never imply Writer gains."""
from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from codey.operations.task_context import ProjectTaskContextBuilder
from codey.providers import controls
from codey.runtime.core.run_result import RunResult
from codey.workspace.changes import ChangeTracker
from codey.workspace.task_changes import collect_task_changes
from tests.manual.coding_pipeline_value_ab import grade, save, write_files
from tests.manual.cross_model_quality_ab import prepare, review
from tests.manual.cross_model_quality_cases import FIXTURE_BY_NAME

MODELS = ("cline-free/solar-mini4", "nvidia/nemotron-3.5-lightning-free", "reflection/beam-free")


def seed_candidate(root, fixture, flawed):
    baseline = {**fixture.case.files, **(fixture.reference if flawed else {})}
    write_files(root, baseline)
    tracker = ChangeTracker(root)
    candidate = fixture.mutants[0] if flawed else fixture.reference
    if all(baseline.get(path) == text for path, text in candidate.items()):
        raise ValueError("control candidate has no diff; cannot calibrate Review")
    for path, text in candidate.items():
        tracker.capture_before(path)
        write_files(root, {path: text})
        tracker.capture_after(path)
    return tracker


def run_control(fixture, model, mode, flawed):
    records = []
    row = {"case": fixture.case.name, "model": model, "mode": mode,
           "candidate": "mutant" if flawed else "reference", "natural_writer_output": False}
    with tempfile.TemporaryDirectory(prefix="codey-quality-control-") as temporary:
        root, state = Path(temporary) / "project", Path(temporary) / "state"
        root.mkdir()
        try:
            if mode == "review":
                tracker = seed_candidate(root, fixture, flawed)
                row["changes"] = collect_task_changes(root, tracker)
                _, row["review"] = review(
                    fixture.case, root, state, tracker, RunResult("Controlled candidate for independent Review."),
                    None, SimpleNamespace(reviewer=model), records, {"events": []},
                )
                row["quality"] = grade(root, fixture.case)
            else:
                write_files(root, fixture.case.files)
                project_map = ProjectTaskContextBuilder().build(
                    project=root, task=fixture.case.task, session_id="advisor-control", run_id="map",
                    continue_task=False, provider_session_changed=False,
                ).project_map
                brief = prepare(None, fixture.case, root, project_map, "audit",
                                SimpleNamespace(advisor=model, audit_turns=8), records, row)
                row["brief_available"] = brief is not None
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            row["providers"] = []
            for purpose, observed, mark in records:
                row["providers"].append({"purpose": purpose, "model": observed.model, **observed.evidence(mark)})
                observed.close()
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("review", "audit"), required=True)
    parser.add_argument("--model", action="append", choices=MODELS)
    parser.add_argument("--case", action="append", choices=tuple(name for name, f in FIXTURE_BY_NAME.items() if f.mutants))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    payload = {"setup": "Controlled candidates; independent oracles never shown to models", "rows": []}
    controls.begin_task_context("cross-model-quality-controls")
    try:
        for name in args.case or ("config-isolation", "atomic-transfer"):
            for flawed in ((False, True) if args.mode == "review" else (False,)):
                for model in args.model or MODELS:
                    row = run_control(FIXTURE_BY_NAME[name], model, args.mode, flawed)
                    payload["rows"].append(row)
                    save(args.output, payload)
                    print(json.dumps({"case": name, "mode": args.mode, "candidate": row["candidate"],
                                      "model": model, "brief_available": row.get("brief_available"),
                                      "review": row.get("review", {}).get("result"), "error": row.get("error")},
                                     ensure_ascii=False), flush=True)
    finally:
        controls.end_task_context()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
