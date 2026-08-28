"""Manual production-spine A/B for Ghost Work Queue continuation.

The harness uses the production ``TaskRunner`` entry point. Queue consumption is
real, while mode bodies are safe stubs so the probe does not edit the repository
or run shell commands. Results are written atomically after every row.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from codey.agents.runner import RunResult
import codey.ghost.work_queue as work_queue_module
from codey.providers.registry import connect_fresh_provider_tab, provider_ids
from codey.research.ledger import ResearchLedger
from codey.research.object_model import build_research_record
from codey.research.pipeline import ResearchIterationRun
from codey.research.report_quality import review_report_quality
from codey.research.runner import ResearchRunResult
from codey.reviews.core import ReviewResult
from codey.app import server
from codey.app.task_runner import TaskRequest, TaskRunner
from codey.runs.work_checkpoint import WorkCheckpointStore


RESULTS_DIR = Path(__file__).resolve().parent / "results"
ARMS = ("baseline", "queue")


@dataclass(frozen=True)
class WorkQueueCase:
    name: str
    task: str
    expected_mode: str
    seed_kind: str = ""
    project: bool = False
    has_reviewable_diff: bool = False


DEFAULT_CASES = (
    WorkQueueCase(
        name="no-queue-continue-chat",
        task="继续",
        expected_mode="chat",
    ),
    WorkQueueCase(
        name="research-item",
        task="继续",
        expected_mode="research",
        seed_kind="research",
    ),
    WorkQueueCase(
        name="project-followup",
        task="继续",
        expected_mode="project",
        seed_kind="project_followup",
        project=True,
        has_reviewable_diff=True,
    ),
    WorkQueueCase(
        name="review-item",
        task="下一个",
        expected_mode="review",
        seed_kind="review",
        project=True,
        has_reviewable_diff=True,
    ),
    WorkQueueCase(
        name="explicit-request-does-not-consume",
        task="继续查 pytest 最近变化",
        expected_mode="chat",
        seed_kind="research",
    ),
)


class _MainProvider:
    name = "Stub Main Provider"
    location = "stub://ghost-work-queue"

    def __init__(self, reply: str = "stub chat reply") -> None:
        self.reply = reply
        self.prompts: list[str] = []
        self.closed = False

    def new_chat(self, timeout: float | None = None) -> None:
        del timeout

    def send(self, text: str, timeout: float | None = None) -> str:
        del timeout
        self.prompts.append(text)
        return self.reply

    def close(self) -> None:
        self.closed = True


def run_cases(
    *,
    provider_id: str,
    cases: tuple[WorkQueueCase, ...] = DEFAULT_CASES,
    provider_factory: Callable[[str], Any] | None = None,
    output: Path | None = None,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    _write_progress(output, provider_id, rows, complete=False)
    for case in cases:
        rows.append(
            _run_case(
                case,
                provider_id=provider_id,
                arm="baseline",
                provider_factory=provider_factory,
            )
        )
        _write_progress(output, provider_id, rows, complete=False)
        rows.append(
            _run_case(
                case,
                provider_id=provider_id,
                arm="queue",
                provider_factory=provider_factory,
            )
        )
        _write_progress(output, provider_id, rows, complete=False)
    payload = _payload(provider_id, rows, complete=True)
    _write_progress(output, provider_id, rows, complete=True)
    return payload


def _run_case(
    case: WorkQueueCase,
    *,
    provider_id: str,
    arm: str,
    provider_factory: Callable[[str], Any] | None,
) -> dict[str, Any]:
    started = time.time()
    provider = None
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        project = root / "project"
        if case.project:
            project.mkdir()
            (project / "app.py").write_text("value = 1\n", encoding="utf-8")
        state = server.State(root / "state")
        session_id = f"{arm}-{case.name}"
        if arm == "queue":
            _seed_case_item(state, case, project if case.project else None, session_id=session_id)
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
                changed=changed,
                checks_passed=changed,
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

        runner = TaskRunner(
            state,
            agent_run=agent_run,
            collect_changes=collect_changes,
            run_review=run_review,
            capture_provider_failure=server.capture_provider_failure,
            project_facts=state.project_facts,
            work_checkpoints=state.work_checkpoints,
            run_ledgers=state.run_ledgers,
            run_traces=state.run_traces,
            evidence_ledgers=state.evidence_ledgers,
            managed_outputs=state.managed_outputs,
            knowledge_store=state.knowledge_store,
            is_git_repository=lambda _project: True,
            ghost_router_provider_factory=None,
        )

        def research_task(**_kwargs):
            nonlocal research_calls
            research_calls += 1
            question = _research_question_from_task(str(_kwargs.get("task") or ""))
            record = _proof_record(question, run_id=str(_kwargs.get("run_id") or ""))
            return ResearchIterationRun(
                result=ResearchRunResult(
                    question,
                    "stub research done",
                    "done",
                    1,
                    synthesis_id="note-result",
                    citation_map=[{"claim": "x"}],
                    research_record=record,
                )
            )

        runner._run_research_iteration = research_task
        try:
            provider = provider_factory(provider_id) if provider_factory is not None else _MainProvider()
            with _patched_provider(state, provider):
                runner.run(
                    TaskRequest(
                        session_id=session_id,
                        project=str(project) if case.project else None,
                        task=case.task,
                        max_turns=8,
                        continue_task=False,
                        provider_id=provider_id,
                        intent="auto",
                    )
                )
            state.wait_for_ghost_sleep(timeout=2)
            error = ""
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                if provider is not None:
                    provider.close()
            except Exception:
                pass
        done = dict(state.last_terminal_event or {})
        start = _last_start_event(events)
        observed = _observed_mode(start, done, agent_calls, research_calls, review_calls)
        item_rows = state.ghost_work_queue.list_items() if state.ghost_work_queue is not None else ()
        queue_statuses = [item.status for item in item_rows]
        consumed = bool(any(status in {"done", "blocked", "running"} for status in queue_statuses))
    expected = case.expected_mode
    exact = not error and observed == expected
    return {
        "provider": provider_id,
        "case": case.name,
        "arm": arm,
        "task": case.task,
        "seed_kind": case.seed_kind,
        "expected_mode": expected,
        "observed_mode": observed,
        "exact": exact,
        "error_cost": 0 if exact else _mode_cost(expected, observed),
        "severe_error": _mode_cost(expected, observed) >= 5,
        "error": error,
        "task_start_mode": str(start.get("mode") or ""),
        "task_done_mode": str(done.get("mode") or ""),
        "queue_statuses": queue_statuses,
        "queue_consumed": consumed,
        "agent_permissions": agent_calls,
        "research_calls": research_calls,
        "review_calls": review_calls,
        "elapsed": round(time.time() - started, 3),
    }


def _research_question_from_task(task: str) -> str:
    for line in str(task or "").splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("task:"):
            return stripped.split(":", 1)[1].strip().rstrip(".") or "Research provider recovery"
    return "Research provider recovery"


def _proof_record(question: str, *, run_id: str):
    url = "https://example.com/work-queue-proof"
    claim = "The saved provider recovery question should be researched again with opened evidence."
    source_text = f"{claim} 2026 source note."
    summary = (
        "## 结论\n"
        f"- {claim} [1]\n\n"
        "## 关键证据\n"
        f"- [1] The opened source says {claim}\n\n"
        "## 反证与限制\n"
        "- 未找到强反证；需要持续追踪新的 provider recovery evidence。\n\n"
        "## 来源质量\n"
        "- [1] secondary · web · fresh · example.com\n\n"
        "## 搜索覆盖\n"
        f"- query: {question}\n"
        "- opened: Work queue proof article\n"
        "- skipped: none representative\n\n"
        "## 来源\n"
        f"[1] Work queue proof article - {url}"
    )
    ledger = ResearchLedger()
    ledger.record_search(
        question,
        [
            {
                "title": "Work queue proof article",
                "url": url,
                "snippet": claim,
            }
        ],
    )
    ledger.record_open(
        requested_url=url,
        final_url=url,
        title="Work queue proof article",
        text=source_text,
    )
    prepared = ledger.prepare_evidence_items(
        [
            {
                "claim": claim,
                "source_url": url,
                "excerpt": claim,
                "stance": "supports",
            }
        ],
        fallback_sources=[url],
        fallback_claim=claim,
        fallback_body=source_text,
        note_type="fact",
    )
    if prepared.error:
        raise AssertionError(prepared.error)
    ledger.add_evidence_items(list(prepared.items), note_id="manual-proof")
    quality = review_report_quality(
        summary,
        ledger=ledger,
        opened_sources=ledger.final_url_set(),
        search_result_urls={url},
    )
    if not quality.ok:
        raise AssertionError(quality.message)
    return build_research_record(
        question=question,
        summary=summary,
        ledger=ledger,
        review=quality,
        run_id=run_id or "manual-proof-run",
        session_id="manual-proof-session",
        synthesis_id="manual-proof-synthesis",
        stop_reason="done",
    )


def _seed_case_item(
    state: server.State,
    case: WorkQueueCase,
    project: Path | None,
    *,
    session_id: str,
) -> None:
    if case.seed_kind == "research":
        item = work_queue_module._new_item(
            kind="research",
            status="queued",
            scope="session",
            scope_ref=work_queue_module._session_ref(session_id),
            title="Research the saved provider recovery question",
            why_now="Bounded local open question from continuity.",
            priority=0.8,
            confidence=0.9,
            source="research_note",
            source_ref=f"note-{case.name}",
            evidence_refs=(f"note:{case.name}",),
            run_refs=(),
            now="2999-01-01T00:00:00Z",
        )
    elif case.seed_kind == "project_followup" and project is not None:
        checkpoints = WorkCheckpointStore(state.state_home)
        checkpoint = checkpoints.start(
            run_id=f"old-{case.name}",
            session_id=session_id,
            project=project,
            task="Finish the saved parser follow-up",
        )
        checkpoints.set_status(checkpoint, "interrupted", "error")
        state.ghost_work_queue.sync_from_sources(
            work_checkpoint_store=checkpoints,
            session_id=session_id,
            project=str(project),
        )
        return
    elif case.seed_kind == "review" and project is not None:
        item = work_queue_module._new_item(
            kind="review",
            status="queued",
            scope="project",
            scope_ref=str(project),
            title="Review the current local diff",
            why_now="A bounded local follow-up requested review.",
            priority=0.8,
            confidence=0.9,
            source="user",
            source_ref=f"review-{case.name}",
            evidence_refs=(f"review:{case.name}",),
            run_refs=(),
            now="2999-01-01T00:00:00Z",
        )
    else:
        return
    assert state.ghost_work_queue is not None
    _write_work_snapshot(state.ghost_work_queue, [item])


def _write_work_snapshot(store, items) -> None:
    store.events_path.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "schema_version": work_queue_module.WORK_QUEUE_SCHEMA_VERSION,
        "type": "ghost_work_snapshot",
        "event_id": "manual_work_snapshot",
        "ts": "2999-01-01T00:00:00Z",
        "reason": "manual_seed",
        "items": [item.to_payload() for item in items],
    }
    store.events_path.write_text(
        json.dumps(event, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    assert store.rebuild_from_events()


class _patched_provider:
    def __init__(self, state: server.State, provider: Any) -> None:
        self.state = state
        self.provider = provider
        self.patch = None

    def __enter__(self):
        from unittest import mock

        self.patch = mock.patch.object(self.state, "get_provider", return_value=self.provider)
        return self.patch.__enter__()

    def __exit__(self, exc_type, exc, tb):
        if self.patch is None:
            return False
        return self.patch.__exit__(exc_type, exc, tb)


def _last_start_event(queue) -> dict[str, object]:
    rows: list[dict[str, object]] = []
    while not queue.empty():
        rows.append(queue.get_nowait())
    return next((row for row in rows if row.get("type") == "task_start"), {})


def _observed_mode(
    start: dict,
    done: dict,
    agent_calls: list[str],
    research_calls: int,
    review_calls: int,
) -> str:
    if research_calls and agent_calls:
        return "hybrid"
    if research_calls:
        return "research"
    if agent_calls and agent_calls[-1] == "planning_readonly":
        return "planning_readonly"
    if agent_calls and agent_calls[-1] == "coding_writer":
        return "project"
    if review_calls:
        return "review"
    mode = str(done.get("mode") or start.get("mode") or "").strip()
    if mode == "agent":
        return "project"
    if mode == "planning":
        return "planning_readonly"
    return mode or "chat"


def _mode_cost(expected: str, observed: str) -> int:
    if expected == observed:
        return 0
    risky = {"project", "hybrid"}
    if expected not in risky and observed in risky:
        return 6
    if expected in risky and observed not in risky:
        return 3
    return 1


def _payload(provider_id: str, rows: list[dict[str, Any]], *, complete: bool) -> dict[str, Any]:
    summary = _summarize(rows)
    return {
        "provider": provider_id,
        "complete": complete,
        "ok": bool(complete)
        and bool(rows)
        and summary["queue"]["exact"] == summary["queue"]["total"]
        and summary["delta"]["cost"] >= 0
        and summary["delta"]["severe_errors"] >= 0,
        "summary": summary,
        "rows": rows,
    }


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for arm in ARMS:
        arm_rows = [row for row in rows if row.get("arm") == arm]
        summary[arm] = {
            "total": len(arm_rows),
            "exact": sum(1 for row in arm_rows if row.get("exact")),
            "cost": sum(int(row.get("error_cost") or 0) for row in arm_rows),
            "severe_errors": sum(1 for row in arm_rows if row.get("severe_error")),
        }
    summary["delta"] = {
        "exact": summary["queue"]["exact"] - summary["baseline"]["exact"],
        "cost": summary["baseline"]["cost"] - summary["queue"]["cost"],
        "severe_errors": summary["baseline"]["severe_errors"] - summary["queue"]["severe_errors"],
    }
    return summary


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


def _selected_cases(cases: tuple[WorkQueueCase, ...], names: list[str]) -> tuple[WorkQueueCase, ...]:
    if not names:
        return cases
    wanted = set(names)
    selected = tuple(case for case in cases if case.name in wanted)
    missing = sorted(wanted - {case.name for case in selected})
    if missing:
        raise SystemExit(f"unknown case(s): {', '.join(missing)}")
    return selected


def _self_test() -> None:
    payload = run_cases(provider_id="fake", provider_factory=lambda _provider_id: _MainProvider())
    if not payload["ok"]:
        raise AssertionError(payload)
    print("self-test ok")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run production-spine Ghost Work Queue A/B")
    parser.add_argument("--provider", choices=provider_ids(), default="deepseek")
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)

    if args.self_test:
        _self_test()
        return 0

    provider_id = str(args.provider).strip().lower()
    cases = _selected_cases(DEFAULT_CASES, args.case)
    output = args.output or RESULTS_DIR / f"ghost_work_queue_production_{provider_id}.json"
    payload = run_cases(
        provider_id=provider_id,
        cases=cases,
        provider_factory=lambda provider: connect_fresh_provider_tab(provider, port=args.port),
        output=output,
    )
    print(json.dumps({"ok": bool(payload.get("ok")), "output": str(output)}, ensure_ascii=False))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
