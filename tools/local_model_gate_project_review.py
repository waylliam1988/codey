"""Write -> automatic local review -> optional repair, through one production run."""
from __future__ import annotations

import json
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path

from tools import local_model_gate_attempts as attempts


def _successful_verification(row: dict) -> bool:
    return (row.get("type") == "tool" and row.get("tool_name") == "run" and row.get("ok") is True
            and type(row.get("exit_code")) is int and row["exit_code"] == 0)


def check_project_review_flow(*, rows, requests, model, reviewer_ids, review_read_only,
                              persisted_review, independent_ok, exit_code, reviewer_model=None) -> dict[str, bool]:
    """Independent lifecycle checks; no verdict is inferred from model prose."""
    terminals = [row for row in rows if row.get("type") == "task_done"]
    reviews = [(index, row) for index, row in enumerate(rows)
               if row.get("type") == "review" and isinstance(row.get("review"), dict)]
    terminal = terminals[0] if len(terminals) == 1 else {}
    review_index, review_event = reviews[0] if len(reviews) == 1 else (-1, {})
    review = review_event.get("review", {})
    identities = {(row.get("run_id"), row.get("session_id")) for row in rows
                  if row.get("type") in {"task_start", "tool", "review", "task_done"}}
    metadata_keys = ("verdict", "status", "origin", "finding_count", "attempt_id", "artifact_sha256")
    before = rows[:review_index] if review_index >= 0 else []
    edits = [index for index, row in enumerate(rows) if row.get("type") == "tool"
             and row.get("tool_name") == "edit" and row.get("ok") is True]
    last_edit = max(edits, default=-1)
    return {
        "single_terminal": len(terminals) == 1 and terminal.get("stop_reason") == "done"
            and type(exit_code) is int and exit_code == 0,
        "single_review": len(reviews) == 1,
        "terminal_after_review": len(terminals) == 1 and review_index >= 0
            and rows.index(terminal) > review_index,
        "same_run": len(identities) == 1 and all(run and session for run, session in identities)
            and any(row.get("type") == "task_start" for row in rows),
        "real_review_request": len(reviewer_ids) == 1
            and any(row.get("recorder_id") in reviewer_ids for row in requests),
        "real_writer_request": any(row.get("recorder_id") not in reviewer_ids for row in requests),
        "expected_models": bool(requests) and all(row.get("payload", {}).get("model") ==
            (reviewer_model or model if row.get("recorder_id") in reviewer_ids else model) for row in requests),
        "writer_verified_before_review": any(row.get("type") == "tool" and row.get("tool_name") == "edit"
            and row.get("ok") is True for row in before) and any(_successful_verification(row) for row in before),
        "review_result_complete": review.get("status") == "complete" and review.get("origin") == "fresh"
            and review.get("verdict") in {"approved", "changes_requested"},
        "persisted_result": isinstance(persisted_review, dict) and bool(persisted_review.get("artifact_sha256"))
            and persisted_review.get("self_review") is (not bool(reviewer_model)) and bool(persisted_review.get("model_id"))
            and all(persisted_review.get(key) == review.get(key) == terminal.get("review", {}).get(key)
                    for key in metadata_keys),
        "review_read_only": review_read_only is True,
        "fresh_final_verification": last_edit >= 0 and any(_successful_verification(row) for row in rows[last_edit + 1:])
            and terminal.get("receipt", {}).get("verification", {}).get("checks_passed") is True,
        "independent_files": independent_ok is True,
    }


class ReviewRecordingProvider:
    """Observe files across the actual read-only Reviewer lifetime."""
    def __init__(self, target, directory, project, *, provider=None):
        self.provider = provider if provider is not None else attempts.make_provider(target, directory)
        from tools.local_model_release_gate import fixture_file_hashes

        self.project = project
        self.before = fixture_file_hashes(project)
        self.after = None

    def __getattr__(self, name):
        return getattr(self.provider, name)

    def close(self):
        from tools.local_model_release_gate import fixture_file_hashes

        try:
            self.provider.close()
        finally:
            self.after = fixture_file_hashes(self.project)


@contextmanager
def observe_independent_api_reviewers(target, directory, project, reviewers):
    """Observe production selection; never replace it with a pinned Writer."""
    import uuid
    from unittest.mock import patch

    from codey.providers import api_connections

    original = api_connections.open_selection

    def open_selection(selection):
        provider = original(selection)
        if selection.connection_id != target.provider_id or selection.model_id == target.model:
            return provider
        provider.recorder_id = uuid.uuid4().hex
        provider.runtime._gate_recorder_id = provider.recorder_id
        observed = ReviewRecordingProvider(target, directory, project, provider=provider)
        reviewers.append(observed)
        return observed

    with patch.object(api_connections, "open_selection", open_selection):
        yield


def run_project_review_case(target, directory: Path) -> dict:
    from codey.app.headless_runner import HeadlessRequest, run_headless
    from codey.reviews.core import review_result_payload
    from codey.reviews.persistence import load_recorded_review
    from tools.local_model_release_gate import _make_fixture, _task_for, _verify_fixture, fixture_test_hashes

    config = json.loads((directory / "input.json").read_text(encoding="utf-8"))
    project, state_home = Path(config["project"]), Path(config["state"])
    _make_fixture(project, "edit")
    baseline_tests = fixture_test_hashes(project)
    task, intent, turns = _task_for("edit")
    turns = target.turn_budget or turns
    rows = []
    reviewers = []

    def emit(row):
        rows.append(row)
        with (directory / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")

    def connect(provider_id, **_kwargs):
        if provider_id != target.provider_id:
            raise RuntimeError("project review gate pins selected connection")
        return attempts.make_provider(target, directory)

    def connect_reviewer(provider_id):
        if provider_id != target.provider_id:
            raise RuntimeError("project review gate pins selected reviewer")
        provider = ReviewRecordingProvider(target, directory, project)
        reviewers.append(provider)
        return provider

    started = time.perf_counter()
    try:
        scope = observe_independent_api_reviewers(target, directory, project, reviewers) if target.provider_id != "local" else nullcontext()
        with scope:
            result = run_headless(
                HeadlessRequest(project=project, task=task, provider_id=target.provider_id, intent=intent,
                                model_selection=({"model": target.model} if target.provider_id != "local" else {}),
                                max_turns=turns, state_home=state_home, project_changes_required=True),
                emit_jsonl=emit, connect_provider=connect,
                connect_reviewer=connect_reviewer if target.provider_id == "local" else None,
            )
        independent = _verify_fixture(project, "edit", baseline_tests=baseline_tests)
        recorded = load_recorded_review(state_home, result.session_id, result.run_id)
        persisted = None
        if recorded is not None:
            review = recorded[1]
            persisted = {**review_result_payload(review), "self_review": review.identity.self_review,
                         "model_id": review.identity.model_id}
        provider_path = directory / "provider.jsonl"
        provider_rows = [json.loads(line) for line in provider_path.read_text(encoding="utf-8").splitlines()] if provider_path.exists() else []
        requests = [row for row in provider_rows if row.get("type") == "request"]
        reviewer_ids = tuple(provider.recorder_id for provider in reviewers)
        checks = check_project_review_flow(
            rows=rows, requests=requests, model=target.model, reviewer_ids=reviewer_ids,
            review_read_only=bool(reviewers) and all(p.after is not None and p.before == p.after for p in reviewers),
            persisted_review=persisted, independent_ok=independent["ok"], exit_code=result.exit_code,
            reviewer_model=reviewers[0].model if reviewers and target.provider_id != "local" else None,
        )
        ok = all(checks.values())
        return {"case": "project_review", "ok": ok, "seconds": round(time.perf_counter() - started, 3),
                "stop_reason": result.stop_reason, "exit_code": result.exit_code,
                "run_id": result.run_id, "session_id": result.session_id,
                "checks": checks, "verification": independent, "work_correct": independent["ok"],
                "review_requests": sum(row.get("recorder_id") in reviewer_ids for row in requests),
                "review_status": persisted.get("status") if persisted else "missing",
                "writer_model": target.model, "reviewer_model": reviewers[0].model if reviewers else "",
                "failure_stage": "" if ok else "project_review_flow"}
    except Exception as exc:  # noqa: BLE001 - live gate records failures
        return {"case": "project_review", "ok": False, "failure_stage": "agent_exception",
                "error": f"{type(exc).__name__}: {exc}"}
    finally:
        (directory / "events.jsonl").touch(exist_ok=True)
