"""Manual production-spine A/B for Ghost Work Queue continuation.

The harness uses the production ``task_entry`` entry point. Queue consumption is
real, while mode bodies are safe stubs so the probe does not edit the repository
or run shell commands. Results are written atomically after every row.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TypedDict
from unittest import mock

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from codey.agents.request import AgentRequest
from codey.app.context import AppContext
from codey.app.event_bus import EventSubscriber
from codey.app.task_services import build_task_deps
from codey.durable.input_receipts import read_input_receipt
from codey.ghost.payload_types import mapping_fields
from codey.ghost.work_queue import GhostWorkQueueStore
from codey.ghost.work_queue_model import WORK_QUEUE_SCHEMA_VERSION, GhostWorkItem, _session_ref
from codey.ghost.work_queue_sources import new_item
from codey.operations.task_entry import run_task_submission
from codey.providers.base import ChatProvider
from codey.providers.registry import connect_fresh_provider_tab, provider_ids
from codey.research.ledger import ResearchLedger
from codey.research.object_model import ResearchRecord, build_research_record
from codey.research.pipeline import ResearchIterationRun
from codey.research.report_quality import review_report_quality
from codey.research.run_result import ResearchRunResult
from codey.reviews.core import ReviewResult
from codey.runs.work_checkpoint import WorkCheckpointStore
from codey.runtime.core.run_result import RunResult
from codey.task.model import TaskSubmission

RESEARCH_ITERATION = "codey.operations.research_iteration.run_research_iteration"


RESULTS_DIR = Path(__file__).resolve().parent / "results"
ARMS = ("baseline", "queue")


class ProbeRow(TypedDict):
    provider: str
    case: str
    arm: str
    task: str
    seed_kind: str
    expected_mode: str
    observed_mode: str
    exact: bool
    error_cost: int
    severe_error: bool
    error: str
    task_start_mode: str
    task_done_mode: str
    queue_statuses: list[str]
    queue_consumed: bool
    agent_permissions: list[str]
    research_calls: int
    review_calls: int
    elapsed: float


class ArmSummary(TypedDict):
    total: int
    exact: int
    cost: int
    severe_errors: int


class DeltaSummary(TypedDict):
    exact: int
    cost: int
    severe_errors: int


class ProbeSummary(TypedDict):
    baseline: ArmSummary
    queue: ArmSummary
    delta: DeltaSummary


class ProbePayload(TypedDict):
    provider: str
    complete: bool
    ok: bool
    summary: ProbeSummary
    rows: list[ProbeRow]


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
    provider_factory: Callable[[str], ChatProvider] | None = None,
    output: Path | None = None,
) -> ProbePayload:
    rows: list[ProbeRow] = []
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
    provider_factory: Callable[[str], ChatProvider] | None,
) -> ProbeRow:
    started = time.time()
    provider: ChatProvider | None = None
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        project = root / "project"
        if case.project:
            project.mkdir()
            (project / "app.py").write_text("value = 1\n", encoding="utf-8")
        state = AppContext(root / "state")
        session_id = f"{arm}-{case.name}"
        if arm == "queue":
            _seed_case_item(state, case, project if case.project else None, session_id=session_id)
        events = state.subscribe()
        agent_calls: list[str] = []
        research_calls = 0
        review_calls = 0

        def agent_run(request: AgentRequest) -> RunResult:
            permission = request.permission_profile
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

        def collect_changes(*_args: object, **_kwargs: object) -> dict[str, object]:
            if case.has_reviewable_diff:
                return {
                    "ok": True,
                    "changed_count": 1,
                    "files": [{"path": "app.py", "status": "M"}],
                    "diff": "diff --git a/app.py b/app.py\n-value = 1\n+value = 2\n",
                }
            return {"ok": True, "changed_count": 0, "files": [], "diff": ""}

        def run_review(**_kwargs: object) -> tuple[str, ReviewResult]:
            nonlocal review_calls
            review_calls += 1
            return "reviewer", ReviewResult("approved", "Looks good", [])

        def research_task(*_args: object, **_kwargs: object) -> ResearchIterationRun:
            nonlocal research_calls
            research_calls += 1
            task = _kwargs.get("task", "")
            run_id = _kwargs.get("run_id", "")
            if not isinstance(task, str) or not isinstance(run_id, str):
                raise TypeError("research task and run_id must be strings")
            question = _research_question_from_task(task)
            record = _proof_record(question, run_id=run_id)
            citation_map: list[dict[str, object]] = [{"claim": "x"}]
            return ResearchIterationRun(
                result=ResearchRunResult(
                    question,
                    "stub research done",
                    "done",
                    1,
                    synthesis_id="note-result",
                    citation_map=citation_map,
                    research_record=record,
                )
            )

        try:
            provider = provider_factory(provider_id) if provider_factory is not None else _MainProvider()

            def connect_provider(_provider_id: str) -> ChatProvider:
                assert provider is not None
                return provider

            def is_git_repository(_project: str | Path) -> bool:
                return True

            runner = replace(
                build_task_deps(
                    state, agent_run=agent_run, collect_changes=collect_changes,
                    collect_review_changes=collect_changes, connect_provider=connect_provider,
                ),
                run_review=run_review, is_git_repository=is_git_repository,
            )
            with mock.patch(RESEARCH_ITERATION, new=research_task):
                run_task_submission(runner,
                    TaskSubmission(
                        session_id=session_id,
                        project=str(project) if case.project else None,
                        task=case.task,
                        max_turns=8,
                        continue_task=False,
                        provider_id=provider_id,
                        intent="auto",
                    )
                )
            if not state.wait_for_ghost_sleep(timeout=30):
                raise TimeoutError("ghost sleep did not finish")
            error = ""
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                if provider is not None:
                    provider.close()
            except Exception:
                pass
            try:
                if not state.close():
                    state.wait_for_ghost_sleep(timeout=30)
                    state.close()
            except Exception as exc:
                if not error:
                    error = f"{type(exc).__name__}: {exc}"
        done = dict(state.run_registry.last_terminal_event() or {})
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
        "task_start_mode": _event_mode(start),
        "task_done_mode": _event_mode(done),
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


def _proof_record(question: str, *, run_id: str) -> ResearchRecord:
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
    state: AppContext,
    case: WorkQueueCase,
    project: Path | None,
    *,
    session_id: str,
) -> None:
    if case.seed_kind == "research":
        item = new_item(
            kind="research",
            status="queued",
            scope="session",
            scope_ref=_session_ref(session_id),
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
        run_id = f"old-{case.name}"
        # An interrupted prior run must originate from real application intake.
        # A checkpoint alone cannot authorize its own Ghost source eligibility.
        with state.durable.control_line():
            state.durable.admit_input(
                conversation_id=session_id, request_id=f"input-{run_id}", task_id=run_id,
                text="Finish the saved parser follow-up", provider_snapshot={},
                task_state={"project": str(project)},
            )
            assert state.state_home is not None
            assert read_input_receipt(state.state_home / "durable.sqlite3", run_id, "ghost") is True
            state.durable.claim_task(run_id)
            state.durable.start_task(run_id, state=dict(state.durable.task(run_id).state))
            state.durable.cancel_task(run_id, reason="interrupted before follow-up")
        checkpoints = WorkCheckpointStore(state.state_home)
        checkpoint = checkpoints.start(
            run_id=run_id,
            session_id=session_id,
            project=project,
            task="Finish the saved parser follow-up",
        )
        assert checkpoint is not None
        checkpoints.set_status(checkpoint, "interrupted", "error")
        assert state.ghost_work_queue is not None
        state.ghost_work_queue.sync_from_sources(
            work_checkpoint_store=checkpoints,
            session_id=session_id,
            project=str(project),
        )
        return
    elif case.seed_kind == "review" and project is not None:
        item = new_item(
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


def _write_work_snapshot(store: GhostWorkQueueStore, items: Sequence[GhostWorkItem]) -> None:
    event: dict[str, object] = {
        "schema_version": WORK_QUEUE_SCHEMA_VERSION,
        "type": "ghost_work_snapshot",
        "event_id": "manual_work_snapshot",
        "ts": "2999-01-01T00:00:00Z",
        "reason": "manual_seed",
        "items": [item.to_payload() for item in items],
    }
    with store.records.transaction():
        store.records.append((event,), replace=True)
        store.records.replace_rows(item.to_payload() for item in items)
    assert store.rebuild_from_events()


def _last_start_event(queue: EventSubscriber) -> dict[str, object]:
    rows: list[dict[str, object]] = []
    while not queue.empty():
        payload: object = queue.get_nowait()
        if not mapping_fields(payload):
            raise TypeError("event payload must be an object with string keys")
        rows.append(dict(payload))
    return next((row for row in rows if row.get("type") == "task_start"), {})


def _observed_mode(
    start: Mapping[str, object],
    done: Mapping[str, object],
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
    mode = (_event_mode(done) or _event_mode(start)).strip()
    if mode == "agent":
        return "project"
    if mode == "planning":
        return "planning_readonly"
    return mode or "chat"


def _event_mode(payload: Mapping[str, object]) -> str:
    mode = payload.get("mode", "")
    if mode is None:
        return ""
    if not isinstance(mode, str):
        raise TypeError("event mode must be a string")
    return mode


def _mode_cost(expected: str, observed: str) -> int:
    if expected == observed:
        return 0
    risky = {"project", "hybrid"}
    if expected not in risky and observed in risky:
        return 6
    if expected in risky and observed not in risky:
        return 3
    return 1


def _payload(provider_id: str, rows: list[ProbeRow], *, complete: bool) -> ProbePayload:
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


def _summarize(rows: list[ProbeRow]) -> ProbeSummary:
    def arm_summary(arm: str) -> ArmSummary:
        arm_rows = [row for row in rows if row.get("arm") == arm]
        return {
            "total": len(arm_rows),
            "exact": sum(1 for row in arm_rows if row["exact"]),
            "cost": sum(row["error_cost"] for row in arm_rows),
            "severe_errors": sum(1 for row in arm_rows if row["severe_error"]),
        }
    baseline, queue = arm_summary("baseline"), arm_summary("queue")
    return {
        "baseline": baseline,
        "queue": queue,
        "delta": {
            "exact": queue["exact"] - baseline["exact"],
            "cost": baseline["cost"] - queue["cost"],
            "severe_errors": baseline["severe_errors"] - queue["severe_errors"],
        },
    }


def _write_report(path: Path, payload: ProbePayload) -> None:
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
    rows: list[ProbeRow],
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


class _Options(argparse.Namespace):
    provider: str
    port: int
    case: list[str]
    output: Path | None
    self_test: bool


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run production-spine Ghost Work Queue A/B")
    parser.add_argument("--provider", choices=provider_ids(), default="deepseek")
    parser.add_argument("--port", type=int, default=9222)
    case_names: list[str] = []
    parser.add_argument("--case", action="append", default=case_names)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--self-test", action="store_true")
    args = _Options()
    parser.parse_args(argv, namespace=args)

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
    result: dict[str, object] = {"ok": payload["ok"], "output": str(output)}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
