"""Manual production-spine A/B for automatic routing.

This harness uses the production ``task_entry`` path and ``codey.ghost.router``
implementation. The front router can be a live web provider, while the actual
mode bodies use safe stubs so the A/B does not edit the repository or run shell
commands.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from unittest import mock

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from codey.agents.runner import RunResult
from codey.providers.registry import connect_fresh_provider_tab, provider_ids
from codey.research.pipeline import ResearchIterationRun
from codey.research.runner import ResearchRunResult
from codey.reviews.core import ReviewResult
from codey.app import server
from codey.task.model import TaskSubmission
from codey.operations.task_entry import TaskRunDeps, run_task_submission
from tests.manual.ghost_router_ab import (
    DEFAULT_CASES,
    RESULTS_DIR,
    FakeProvider,
    RouterCase,
    load_cases,
    route_error_cost,
    summarize_rows,
)

RESEARCH_ITERATION = "codey.operations.research_flow.run_research_iteration"


class _MainProvider:
    name = "Stub Main Provider"
    location = "stub://main"

    def new_chat(self, timeout: float | None = None) -> None:
        del timeout

    def send(self, text: str, timeout: float | None = None) -> str:
        del text, timeout
        return "stub chat reply"

    def close(self) -> None:
        return None


def run_cases(
    *,
    provider_id: str,
    cases: tuple[RouterCase, ...],
    router_provider_factory,
    output: Path | None = None,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    _write_progress(output, provider_id, rows, complete=False)
    for case in cases:
        rows.append(_run_case(
            case,
            provider_id=provider_id,
            arm="baseline",
            router_provider_factory=None,
        ))
        _write_progress(output, provider_id, rows, complete=False)
        rows.append(_run_case(
            case,
            provider_id=provider_id,
            arm="router",
            router_provider_factory=router_provider_factory,
        ))
        _write_progress(output, provider_id, rows, complete=False)
    payload = _payload(provider_id, rows, complete=True)
    _write_progress(output, provider_id, rows, complete=True)
    return payload


def _payload(provider_id: str, rows: list[dict[str, Any]], *, complete: bool) -> dict[str, Any]:
    summary = summarize_rows(rows)
    return {
        "provider": provider_id,
        "complete": complete,
        "ok": bool(summary["router"]["exact"] == summary["router"]["total"])
        and summary["router"]["cost"] < summary["baseline"]["cost"],
        "summary": summary,
        "rows": rows,
    }


def _run_case(
    case: RouterCase,
    *,
    provider_id: str,
    arm: str,
    router_provider_factory,
) -> dict[str, Any]:
    started = time.time()
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        project = root / "project"
        if case.project:
            project.mkdir()
            (project / "app.py").write_text("value = 1\n", encoding="utf-8")
        state = server.State(root / "state")
        events = state.subscribe()
        agent_calls: list[str] = []
        research_calls = 0
        review_calls = 0

        def agent_run(*_args, **kwargs):
            permission = str(kwargs.get("permission_profile") or "")
            agent_calls.append(permission)
            changed = permission == "coding_writer"
            return RunResult(
                "stub writer done" if changed else "stub plan done",
                "done",
                1,
                checks_passed=changed,
                changed=changed,
                checks_ran=changed,
            )

        def collect_changes(*_args, **_kwargs) -> dict:
            if case.has_reviewable_diff:
                return {
                    "ok": True,
                    "changed_count": 1,
                    "files": [{"path": "app.py", "status": "M"}],
                    "diff": "diff --git a/app.py b/app.py\n-value = 1\n+value = 2\n",
                }
            return {"ok": True, "changed_count": 0, "files": [], "diff": ""}

        def run_review(**_kwargs):
            nonlocal review_calls
            review_calls += 1
            return "reviewer", ReviewResult("approved", "Looks good", [])

        runner = TaskRunDeps(state=state,
            agent_run=agent_run,
            collect_changes=collect_changes,
            run_review=run_review,
            capture_provider_failure=server.capture_provider_failure,
            project_facts=state.project_facts,
            work_checkpoints=state.work_checkpoints,
            run_ledgers=state.run_ledgers,
            managed_outputs=state.managed_outputs,
            knowledge_store=state.knowledge_store,
            is_git_repository=lambda _project: True,
            ghost_router_provider_factory=router_provider_factory,
        )

        def research_task(*_args, **_kwargs):
            nonlocal research_calls
            research_calls += 1
            return ResearchIterationRun(
                result=ResearchRunResult("q", "stub research done", "done", 1)
            )

        try:
            with (
                _patched_provider(state),
                mock.patch(RESEARCH_ITERATION, side_effect=research_task),
            ):
                run_task_submission(runner, TaskSubmission(
                    session_id=f"{arm}-{case.name}",
                    project=str(project) if case.project else None,
                    task=case.task,
                    max_turns=8,
                    continue_task=False,
                    provider_id=provider_id,
                    intent="auto",
                ))
            state.wait_for_ghost_sleep(timeout=2)
            error = ""
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        emitted = _drain(events)
        start = next((event for event in emitted if event.get("type") == "task_start"), {})
        done = dict(state.last_terminal_event or {})
        route_record = _latest_route_record(state)
        observed = _observed_mode(start, done, agent_calls, research_calls, review_calls)
    exact = not error and observed == case.expected_mode
    return {
        "provider": provider_id,
        "case": case.name,
        "arm": arm,
        "expected_mode": case.expected_mode,
        "observed_mode": observed,
        "exact": exact,
        "parse_ok": True,
        "error_cost": 0 if exact else route_error_cost(case.expected_mode, observed),
        "severe_error": route_error_cost(case.expected_mode, observed) >= 5,
        "error": error,
        "task_start_mode": str(start.get("mode") or ""),
        "task_done_mode": str(done.get("mode") or ""),
        "route_selected_mode": str(route_record.get("selected_mode") or ""),
        "route_final_mode": str(route_record.get("final_mode") or ""),
        "route_confidence": route_record.get("confidence", 0.0),
        "route_skipped_reason": str(route_record.get("skipped_reason") or ""),
        "route_reason": str(route_record.get("reason") or ""),
        "route_diagnostics": route_record.get("diagnostics") or [],
        "route_warnings": route_record.get("warnings") or [],
        "agent_permissions": agent_calls,
        "research_calls": research_calls,
        "review_calls": review_calls,
        "elapsed": round(time.time() - started, 3),
    }


def _latest_route_record(state: server.State) -> dict[str, object]:
    router = getattr(state, "ghost_router", None)
    if router is None:
        return {}
    try:
        records = router.export_state()["router"]["records"]
    except Exception:
        return {}
    return dict(records[-1]) if records else {}


class _patched_provider:
    def __init__(self, state: server.State) -> None:
        self.state = state
        self.patch = None

    def __enter__(self):
        from unittest import mock

        self.patch = mock.patch.object(self.state, "get_provider", return_value=_MainProvider())
        return self.patch.__enter__()

    def __exit__(self, exc_type, exc, tb):
        if self.patch is None:
            return False
        return self.patch.__exit__(exc_type, exc, tb)


def _observed_mode(
    start: dict,
    done: dict,
    agent_calls: list[str],
    research_calls: int,
    review_calls: int,
) -> str:
    if review_calls:
        return "review"
    if research_calls and agent_calls:
        return "hybrid"
    if research_calls:
        return "research"
    if agent_calls and agent_calls[-1] == "planning_readonly":
        return "planning_readonly"
    if agent_calls and agent_calls[-1] == "coding_writer":
        return "project"
    mode = str(done.get("mode") or start.get("mode") or "").strip()
    if mode == "agent":
        return "project"
    if mode == "planning":
        return "planning_readonly"
    return mode or "chat"


def _drain(queue) -> list[dict]:
    rows: list[dict] = []
    while not queue.empty():
        rows.append(queue.get_nowait())
    return rows


def _selected_cases(cases: tuple[RouterCase, ...], names: list[str]) -> tuple[RouterCase, ...]:
    if not names:
        return cases
    wanted = set(names)
    selected = tuple(case for case in cases if case.name in wanted)
    missing = sorted(wanted - {case.name for case in selected})
    if missing:
        raise SystemExit(f"unknown case(s): {', '.join(missing)}")
    return selected


def _write_report(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _write_progress(
    path: Path | None,
    provider_id: str,
    rows: list[dict[str, Any]],
    *,
    complete: bool,
) -> None:
    if path is None:
        return
    _write_report(path, _payload(provider_id, rows, complete=complete))


def _self_test() -> None:
    payload = run_cases(
        provider_id="fake",
        cases=load_cases(),
        router_provider_factory=lambda _provider_id: FakeProvider(),
    )
    if not payload["ok"]:
        raise AssertionError(payload)
    print("self-test ok")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run production-spine automatic routing A/B")
    parser.add_argument("--provider", choices=provider_ids(), default="deepseek")
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)

    if args.self_test:
        _self_test()
        return 0

    provider_id = str(args.provider).strip().lower()
    cases = _selected_cases(load_cases(args.cases), args.case)
    output = args.output or RESULTS_DIR / f"ghost_router_production_{provider_id}.json"
    payload = run_cases(
        provider_id=provider_id,
        cases=cases,
        router_provider_factory=lambda provider: connect_fresh_provider_tab(provider, port=args.port),
        output=output,
    )
    print(json.dumps({"ok": bool(payload.get("ok")), "output": str(output)}, ensure_ascii=False))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
