"""Production-spine A/B for Research Interest Queue consumption.

Candidate generation is deterministic and local. The harness uses the
production TaskRunner claim path, while Research/Project/Review bodies are safe
stubs so this probe does not write project files or run shell commands.
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
from codey.knowledge.note import KnowledgeNote
from codey.knowledge.research_interest import build_research_interest_candidates
from codey.knowledge.store import KnowledgeStore
from codey.research.ledger import ResearchLedger
from codey.research.object_model import build_research_record
from codey.research.pipeline import ResearchIterationRun
from codey.research.report_quality import review_report_quality
from codey.providers.registry import connect_fresh_provider_tab, provider_ids
from codey.research.runner import ResearchRunResult
from codey.reviews.core import ReviewResult
from codey.app import server
from codey.app.task_runner import TaskRequest, TaskRunner


RESULTS_DIR = Path(__file__).resolve().parent / "results"
ARMS = ("baseline", "queue")


@dataclass(frozen=True)
class ResearchInterestCase:
    name: str
    task: str
    expected_mode: str
    seed_kind: str = ""
    expected_queue_status: str = ""
    research_proof: bool = True


DEFAULT_CASES = (
    ResearchInterestCase(
        name="no-queue-continue-chat",
        task="继续",
        expected_mode="chat",
    ),
    ResearchInterestCase(
        name="research-note-open-question",
        task="继续",
        expected_mode="research",
        seed_kind="research_note",
        expected_queue_status="done",
    ),
    ResearchInterestCase(
        name="strong-concept-open-question",
        task="continue",
        expected_mode="research",
        seed_kind="concept_strong",
        expected_queue_status="done",
    ),
    ResearchInterestCase(
        name="weak-concept-remains-candidate",
        task="continue",
        expected_mode="chat",
        seed_kind="concept_weak",
        expected_queue_status="candidate",
    ),
    ResearchInterestCase(
        name="contentful-continue-does-not-consume",
        task="继续查 copper supply 的最新变化",
        expected_mode="chat",
        seed_kind="concept_strong",
        expected_queue_status="queued",
    ),
    ResearchInterestCase(
        name="research-without-proof-blocks",
        task="下一个",
        expected_mode="research",
        seed_kind="concept_strong",
        expected_queue_status="blocked",
        research_proof=False,
    ),
)


class _MainProvider:
    name = "Stub Main Provider"
    location = "stub://ghost-research-interest"

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
    cases: tuple[ResearchInterestCase, ...] = DEFAULT_CASES,
    provider_factory: Callable[[str], Any] | None = None,
    output: Path | None = None,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    _write_progress(output, provider_id, rows, complete=False)
    for case in cases:
        rows.append(_run_case(
            case,
            provider_id=provider_id,
            arm="baseline",
            provider_factory=provider_factory,
        ))
        _write_progress(output, provider_id, rows, complete=False)
        rows.append(_run_case(
            case,
            provider_id=provider_id,
            arm="queue",
            provider_factory=provider_factory,
        ))
        _write_progress(output, provider_id, rows, complete=False)
    payload = _payload(provider_id, rows, complete=True)
    _write_progress(output, provider_id, rows, complete=True)
    return payload


def _run_case(
    case: ResearchInterestCase,
    *,
    provider_id: str,
    arm: str,
    provider_factory: Callable[[str], Any] | None,
) -> dict[str, Any]:
    started = time.time()
    provider = None
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        state = server.State(root / "state")
        state.knowledge_store = KnowledgeStore(root / "knowledge")
        session_id = f"{arm}-{case.name}"
        if arm == "queue":
            _seed_case_interest(state, case, session_id=session_id)
        events = state.subscribe()
        agent_calls: list[str] = []
        research_calls = 0
        review_calls = 0

        def agent_run(*_args, **kwargs):
            permission = str(kwargs.get("permission_profile") or "")
            agent_calls.append(permission)
            changed = permission == "coding_writer"
            return RunResult("stub agent done", "done", 1, changed=changed)

        def run_review(**_kwargs):
            nonlocal review_calls
            review_calls += 1
            return "reviewer", ReviewResult("approved", "Looks good", [])

        runner = TaskRunner(
            state,
            agent_run=agent_run,
            collect_changes=lambda *_args, **_kwargs: {"ok": True, "changed_count": 0, "files": [], "diff": ""},
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
            if case.research_proof:
                question = _research_question_from_task(str(_kwargs.get("task") or ""))
                record = _proof_record(question, run_id=str(_kwargs.get("run_id") or ""))
                return ResearchIterationRun(
                    result=ResearchRunResult(
                        question,
                        "stub research done",
                        "done",
                        1,
                        synthesis_id=f"note-{case.name}",
                        citation_map=[{"claim": "x"}],
                        research_record=record,
                    )
                )
            return ResearchIterationRun(
                result=ResearchRunResult("q", "stub research without proof", "done", 1)
            )

        runner._run_research_iteration = research_task
        try:
            provider = provider_factory(provider_id) if provider_factory is not None else _MainProvider()
            with _patched_provider(state, provider):
                runner.run(TaskRequest(
                    session_id=session_id,
                    project=None,
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
        finally:
            try:
                state.knowledge_store.close()
            except Exception:
                pass
            try:
                if provider is not None:
                    provider.close()
            except Exception:
                pass
        done = dict(state.last_terminal_event or {})
        start = _last_start_event(events)
        observed = _observed_mode(start, done, agent_calls, research_calls, review_calls)
        queue_rows = state.ghost_work_queue.list_items() if state.ghost_work_queue is not None else ()
        queue_statuses = [item.status for item in queue_rows]
        leaked = _leaked_internal_terms(provider, done)
    expected = case.expected_mode
    exact = not error and observed == expected and not leaked
    expected_status_ok = not case.expected_queue_status or case.expected_queue_status in queue_statuses
    if arm == "queue":
        exact = exact and expected_status_ok
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
        "expected_queue_status": case.expected_queue_status,
        "internal_leak": leaked,
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


def _claim_for_question(question: str) -> str:
    lower = question.casefold()
    if "copper" in lower and "helium" in lower:
        return (
            "Copper supply and helium supply are connected through war-related "
            "supply-chain risk."
        )
    return (
        "Provider recovery should be checked again because browser sessions can "
        "change."
    )


def _proof_record(question: str, *, run_id: str):
    url = "https://example.com/research-proof"
    claim = _claim_for_question(question)
    source_text = f"{claim} 2026 source note."
    summary = (
        "## 结论\n"
        f"- {claim} [1]\n\n"
        "## 关键证据\n"
        f"- [1] The opened source says {claim}\n\n"
        "## 反证与限制\n"
        "- 未找到强反证；需要持续追踪新的 primary source。\n\n"
        "## 来源质量\n"
        "- [1] secondary · web · fresh · example.com\n\n"
        "## 搜索覆盖\n"
        f"- query: {question}\n"
        "- opened: Research proof article\n"
        "- skipped: none representative\n\n"
        "## 来源\n"
        f"[1] Research proof article - {url}"
    )
    ledger = ResearchLedger()
    ledger.record_search(question, [{
        "title": "Research proof article",
        "url": url,
        "snippet": claim,
    }])
    ledger.record_open(
        requested_url=url,
        final_url=url,
        title="Research proof article",
        text=source_text,
    )
    prepared = ledger.prepare_evidence_items(
        [{
            "claim": claim,
            "source_url": url,
            "excerpt": claim,
            "stance": "supports",
        }],
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


def _seed_case_interest(
    state: server.State,
    case: ResearchInterestCase,
    *,
    session_id: str,
) -> None:
    assert state.knowledge_store is not None
    assert state.ghost_work_queue is not None
    if case.seed_kind == "research_note":
        state.knowledge_store.write_note(KnowledgeNote.create(
            id=f"synthesis-{case.name}",
            type="synthesis",
            title="Provider recovery synthesis",
            body="Research synthesis.",
            open_questions=["Should provider recovery be checked again?"],
            tags=["research"],
            session_id=session_id,
        ))
    elif case.seed_kind == "concept_strong":
        state.knowledge_store.write_note(KnowledgeNote.create(
            id=f"war-helium-{case.name}",
            type="synthesis",
            title="War and helium supply",
            body="Evidence note.",
            tags=["research"],
            relations=[{"src": "war", "dst": "helium supply", "kind": "affects"}],
            session_id=session_id,
        ))
        state.knowledge_store.write_note(KnowledgeNote.create(
            id=f"war-copper-{case.name}",
            type="synthesis",
            title="War and copper supply",
            body="Evidence note.",
            tags=["research"],
            relations=[{"src": "war", "dst": "copper supply", "kind": "affects"}],
            session_id=session_id,
        ))
    elif case.seed_kind == "concept_weak":
        state.knowledge_store.write_note(KnowledgeNote.create(
            id=f"weak-concept-{case.name}",
            type="synthesis",
            title="Weak concept bridge",
            body="Single note declares two adjacent relations.",
            tags=["research"],
            relations=[
                {"src": "war", "dst": "helium supply", "kind": "affects"},
                {"src": "war", "dst": "copper supply", "kind": "affects"},
            ],
            session_id=session_id,
        ))
    candidates = build_research_interest_candidates(state.knowledge_store, session_id=session_id)
    state.ghost_work_queue.sync_from_sources(
        research_interest_candidates=candidates,
        session_id=session_id,
        run_id=f"seed-{case.name}",
    )


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


def _leaked_internal_terms(provider: Any, done: dict[str, object]) -> bool:
    text = "\n".join([
        *list(getattr(provider, "prompts", []) or []),
        str(done.get("summary") or ""),
    ]).casefold()
    return any(term in text for term in ("ghost", "work queue", "workitem", "concept graph"))


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
        "ok": bool(summary["queue"]["exact"] == summary["queue"]["total"])
        and summary["queue"]["cost"] < summary["baseline"]["cost"],
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


def _selected_cases(cases: tuple[ResearchInterestCase, ...], names: list[str]) -> tuple[ResearchInterestCase, ...]:
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
    parser = argparse.ArgumentParser(description="Run production-spine Research Interest Queue A/B")
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
    output = args.output or RESULTS_DIR / f"ghost_research_interest_queue_production_{provider_id}.json"
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