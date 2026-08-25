"""Narrow production-spine A/B for Ghost Research Continuity (0.4.12).

Both arms see identical seeded local state; the only difference is whether
the ``research_topic_continuity`` context source is admitted into the
Research prompt (continuity arm) or the profile gate stays closed (baseline
arm). The offline probe stubs the Research body exactly like the Research
Interest Queue harness, because the admission chain itself is deterministic
and local. ``--provider <real>`` runs the same matrix as a live smoke with
the real Research loop.

Live-smoke failure attribution (roadmap 0.4.12): every non-ok row is
classified as one of

- ``provider_send_error`` -- the send itself failed (non-timeout error).
- ``native_search_stall_suspected`` -- sends were accepted but no reply ever
  arrived, or the send failed with a timeout-class error. This is a
  provider/native-web-search diagnostic, NOT a Research planner failure.
- ``planner_quality:<stop_reason>`` -- traffic flowed normally but the run
  stopped for Codey-side reasons (protocol/no_progress/max_turns/...).

Transcript policy: digest-first by default (``--transcript-mode
digest-only`` keeps only hashes); pass ``archive`` explicitly to also store
full prompt/reply transcripts under the journal directory. That material
is manual prompt-lab input only; it must never flow back into RunTrace /
EvidenceLedger / ResearchRecord / release claims.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable
from unittest import mock

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.manual.ab_harness_common import TracingProvider
from tests.manual.ab_journal import (
    ABJournalWriter,
    TRANSCRIPT_MODE_ARCHIVE,
    TRANSCRIPT_MODE_DIGEST_ONLY,
    TranscriptReplayCache,
)
from codey.agent import RunResult
from codey.knowledge.note import KnowledgeNote
from codey.knowledge.research_interest import build_research_interest_candidates
from codey.knowledge.store import KnowledgeStore
from codey.research.context import ResearchContext
from codey.research.ledger import ResearchLedger
from codey.research.object_model import build_research_record
from codey.research.pipeline import ResearchIterationRun
from codey.research.report_quality import review_report_quality
from codey.providers.registry import DEFAULT_PROVIDER_ID, connect_fresh_provider_tab, provider_ids
from codey.research.runner import ResearchRunResult
from codey.review import ReviewResult
from codey.task_runner import TaskRequest, TaskRunner
from codey import server

RESULTS_DIR = Path(__file__).resolve().parent / "results"
ARMS = ("baseline", "continuity")
TRANSCRIPT_MODES = ("digest-only", "archive", "off")
LIVE_PROVIDERS = tuple(provider_ids())

_TIMEOUT_ERROR_MARKERS = ("timeout", "timed out", "deadline")


@dataclass(frozen=True)
class ContinuityCase:
    name: str
    seed_note_open_question: str = ""
    seed_prior_claim: bool = False
    seed_preference: bool = False
    drop_ghost_store: bool = False
    # Cases without any local leads stay in plain chat; cases with seeds
    # queue a research item so "continue" routes through Research.
    expected_mode: str = "research"
    # Plain-chat tasks exercise the real provider send path offline; seeded
    # cases use the work-queue continuation keyword instead.
    task: str = "continue"


DEFAULT_CASES = (
    ContinuityCase(name="empty-state-stays-baseline", expected_mode="", task="hello"),
    ContinuityCase(
        name="old-claim-must-be-rechecked",
        seed_note_open_question=(
            "Should provider recovery be re-checked against fresh sources?"
        ),
        seed_prior_claim=True,
    ),
    ContinuityCase(
        name="open-question-improves-next-search",
        seed_note_open_question=(
            "Which 2026 sources track helium supply constraints?"
        ),
    ),
    ContinuityCase(
        name="preference-hint-affects-framing-only",
        seed_note_open_question=(
            "Do shorter summaries change source coverage?"
        ),
        seed_preference=True,
    ),
    ContinuityCase(
        name="missing-ghost-store-still-uses-interests",
        seed_note_open_question=(
            "Should interest leads survive a missing continuity store?"
        ),
        drop_ghost_store=True,
    ),
)


class _MainProvider:
    name = "Stub Main Provider"
    location = "stub://ghost-research-continuity"

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


def classify_outcome(
    *,
    sends: int,
    replies: int,
    send_error_text: str,
    stop_reason: str,
) -> str:
    """Split live failures per the 0.4.12 roadmap attribution rule."""
    lowered = str(send_error_text or "").casefold()
    timeout_like = any(marker in lowered for marker in _TIMEOUT_ERROR_MARKERS)
    if sends == 0 and lowered:
        return "provider_send_error"
    if lowered and timeout_like:
        # Native web search hanging inside the provider page surfaces as a
        # send timeout; it says nothing about Research planner quality.
        return "native_search_stall_suspected"
    if lowered:
        return "provider_send_error"
    if replies == 0:
        return "native_search_stall_suspected"
    if stop_reason != "done":
        return f"planner_quality:{stop_reason}"
    return "ok"


def _task_request_provider_id(provider_id: str) -> str:
    normalized = str(provider_id or "").strip().lower()
    if normalized in LIVE_PROVIDERS:
        return normalized
    # ``fake`` is a harness/reporting label. TaskRunner should still see a
    # real production provider id so offline probes do not exercise an
    # unsupported-provider path.
    return DEFAULT_PROVIDER_ID


def run_cases(
    *,
    provider_id: str,
    cases: tuple[ContinuityCase, ...] = DEFAULT_CASES,
    provider_factory: Callable[[str], Any] | None = None,
    output: Path | None = None,
    live: bool = False,
    max_turns: int = 8,
    journal: ABJournalWriter | None = None,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    _write_progress(output, provider_id, rows, complete=False)
    for case in cases:
        for arm in ARMS:
            row = _run_case(
                case,
                arm=arm,
                provider_id=provider_id,
                provider_factory=provider_factory,
                live=live,
                max_turns=max_turns,
                journal=journal,
            )
            rows.append(row)
            if journal is not None:
                journal.record_case_complete(
                    case=case.name,
                    arm=arm,
                    row={
                        "ok": bool(row.get("exact")),
                        "stop_reason": str(row.get("stop_reason") or ""),
                        "failure_class": str(row.get("failure_class") or ""),
                    },
                )
            _write_progress(output, provider_id, rows, complete=False)
    payload = _payload(provider_id, rows, complete=True)
    if journal is not None:
        journal.record_run_complete(
            rows=len(rows),
            status="done" if payload.get("ok") else "failed",
        )
    _write_progress(output, provider_id, rows, complete=True)
    return payload


def _run_case(
    case: ContinuityCase,
    *,
    arm: str,
    provider_factory: Callable[[str], Any] | None = None,
    provider_id: str = "fake",
    live: bool = False,
    max_turns: int = 8,
    journal: ABJournalWriter | None = None,
) -> dict[str, Any]:
    started = time.time()
    gate_open = arm == "continuity"
    requested_provider = str(provider_id or "").strip().lower() or "fake"
    task_provider = _task_request_provider_id(requested_provider)
    raw_provider = None
    tracing_provider: TracingProvider | None = None
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        state = server.State(root / "state")
        state.knowledge_store = KnowledgeStore(root / "knowledge")
        session_id = f"{arm}-{case.name}"
        project = str(root / "project")
        Path(project).mkdir()
        if case.drop_ghost_store:
            state.ghost_continuity = None
        _seed_case(state, case, session_id=session_id, project=project)
        agent_calls: list[str] = []
        research_contexts: list[ResearchContext] = []
        admission_results: list[tuple[str, dict[str, object] | None]] = []

        def agent_run(*_args, **kwargs):
            permission = str(kwargs.get("permission_profile") or "")
            agent_calls.append(permission)
            return RunResult("stub agent done", "done", 1)

        def run_review(**_kwargs):
            return "reviewer", ReviewResult("approved", "Looks good", [])

        runner = TaskRunner(
            state,
            agent_run=agent_run,
            collect_changes=lambda *_a, **_k: {"ok": True, "changed_count": 0, "files": [], "diff": ""},
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

        original_admission = runner._build_research_topic_continuity

        def spy_admission(**kwargs):
            result = original_admission(**kwargs)
            admission_results.append(result)
            return result

        runner._build_research_topic_continuity = spy_admission

        original_build_context = runner._build_research_context

        def spy_build_context(frame, request, **kwargs):
            context = original_build_context(frame, request, **kwargs)
            research_contexts.append(context)
            return context

        runner._build_research_context = spy_build_context

        if not live:
            def research_task(**kwargs):
                question = str(kwargs.get("task") or "research")
                record = _proof_record(question, run_id=str(kwargs.get("run_id") or ""))
                return ResearchIterationRun(
                    result=ResearchRunResult(
                        question,
                        "stub research done",
                        "done",
                        1,
                        synthesis_id=f"note-{case.name}-{arm}",
                        citation_map=[{"claim": "x"}],
                        research_record=record,
                    )
                )

            runner._run_research_iteration = research_task
        try:
            if provider_factory is not None:
                raw_provider = provider_factory(session_id)
            else:
                raw_provider = _MainProvider()
            # Every provider (real or stub) goes through TracingProvider so
            # send/reply counters and journal transcripts are attributable to
            # one case/arm regardless of the underlying provider class.
            tracing_provider = TracingProvider(
                raw_provider,
                journal=journal,
                case=case.name,
                arm=arm,
            )
            gate_patch = (
                None if gate_open
                else mock.patch("codey.task_runner.allows_context_source", return_value=False)
            )
            with mock.patch.object(state, "get_provider", return_value=tracing_provider):
                request = TaskRequest(
                    session_id=session_id,
                    project=project,
                    task=case.task,
                    max_turns=max_turns,
                    continue_task=False,
                    provider_id=task_provider,
                    intent="auto",
                )
                if gate_patch is not None:
                    with gate_patch:
                        runner.run(request)
                else:
                    runner.run(request)
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
                if raw_provider is not None:
                    raw_provider.close()
            except Exception:
                pass
        done = dict(state.last_terminal_event or {})
        research_ran = bool(research_contexts)

    admitted_text = admission_results[0][0] if admission_results else ""
    payload = admission_results[0][1] if admission_results else None
    context_text = (
        research_contexts[0].topic_continuity_context
        if research_contexts else ""
    )
    stale_rows = [
        row for row in ((payload or {}).get("items") or [])
        if row.get("stale")
    ]
    observed_mode = "research" if research_ran else str(done.get("mode") or "chat")
    leaked = _leaked_internal_terms(admitted_text, str(done.get("summary") or ""))
    exact = not error and not leaked
    if case.expected_mode:
        exact = exact and observed_mode == case.expected_mode
    if gate_open and case.expected_mode == "research":
        exact = exact and bool(admitted_text) and admitted_text == context_text
        exact = exact and bool(payload) and bool((payload or {}).get("admitted"))
    elif case.expected_mode == "research":
        # Baseline arm on a seeded case: research still runs, but with the
        # empty continuity baseline.
        exact = exact and not admitted_text and not context_text
    else:
        exact = exact and not admitted_text
    exact = exact and not leaked
    raw_payload = json.dumps(payload or {}, ensure_ascii=False)
    seeded_question_leaked = bool(
        case.seed_note_open_question
        and case.seed_note_open_question in raw_payload
    )
    row: dict[str, Any] = {
        "case": case.name,
        "arm": arm,
        "gate_open": gate_open,
        "expected_mode": case.expected_mode,
        "observed_mode": observed_mode,
        "requested_provider": requested_provider,
        "task_provider": task_provider,
        "observed_provider": str(done.get("provider") or ""),
        "exact": exact,
        "error": error,
        "admitted": bool(admitted_text),
        "context_carried": bool(context_text) and context_text == admitted_text,
        "digest_only_payload": not seeded_question_leaked,
        "stale_ref_count": len(stale_rows),
        "internal_leak": leaked,
        "elapsed": round(time.time() - started, 3),
    }
    if case.seed_prior_claim:
        row["prior_claim_flagged"] = any(
            stale_row.get("kind") == "prior_claim" for stale_row in stale_rows
        )
    if live:
        stop_reason = str(done.get("stop_reason") or "")
        send_error_text = error
        row["stop_reason"] = stop_reason
        # Counters come from the TracingProvider wrapper, so attribution does
        # not depend on the underlying provider exposing its own counters.
        row["sends"] = int(getattr(tracing_provider, "send_index", 0) or 0)
        row["replies"] = int(getattr(tracing_provider, "reply_count", 0) or 0)
        row["prompt_chars"] = int(getattr(tracing_provider, "prompt_chars", 0) or 0)
        row["reply_chars"] = int(getattr(tracing_provider, "reply_chars", 0) or 0)
        row["failure_class"] = (
            "ok" if exact and not error
            else classify_outcome(
                sends=row["sends"],
                replies=row["replies"],
                send_error_text=send_error_text,
                stop_reason=stop_reason or observed_mode,
            )
        )
    return row


def _leaked_internal_terms(*texts: str) -> bool:
    joined = "\n".join(texts).casefold()
    return any(term in joined for term in ("ghost", "work queue", "workitem", "concept graph"))


def _seed_case(
    state: server.State,
    case: ContinuityCase,
    *,
    session_id: str,
    project: str,
) -> None:
    assert state.knowledge_store is not None
    if case.seed_note_open_question:
        state.knowledge_store.write_note(KnowledgeNote.create(
            id=f"synthesis-{case.name}",
            type="synthesis",
            title="Continuity synthesis",
            body="Research synthesis with an open follow-up.",
            open_questions=[case.seed_note_open_question],
            tags=["research"],
            session_id=session_id,
            # Production syntheses carry the resolved project so scoped
            # continuity queries can find them.
            project=project,
        ))
    if case.seed_prior_claim:
        _seed_prior_claim(state, session_id=session_id, project=project)
    if case.seed_preference:
        from codey.ghost.schema import GhostSignal, GhostSignalParseResult

        assert state.ghost_inbox is not None
        assert state.ghost_hebbian is not None
        created = state.ghost_inbox.ingest_signals(
            GhostSignalParseResult(signals=(GhostSignal(
                kind="style_preference",
                scope="session",
                summary="Prefer concise summaries with numbered sources.",
                evidence_quote="shorter please",
                confidence=0.9,
                metadata={"conflict_key": "reply_length", "value_key": "concise"},
                source="continuity-ab-seed",
            ),), ok=True, provider_id="continuity-ab-seed"),
            session_id=session_id,
            run_id=f"seed-{case.name}",
            user_text="shorter please",
        )
        reviewed = state.ghost_inbox.review_candidate(created[0].id, "accept", reviewed_by="seed")
        if reviewed is not None:
            state.ghost_hebbian.reinforce_candidate(reviewed)
    if state.ghost_continuity is not None and (
        case.seed_note_open_question or case.seed_preference
    ):
        state.ghost_continuity.sync_from_sources(
            knowledge_store=state.knowledge_store,
            hebbian_store=getattr(state, "ghost_hebbian", None),
            session_id=session_id,
            run_id=f"seed-{case.name}",
            project=project,
        )
    # Queue a research work item so the "continue" request routes through
    # the Research pipeline in both arms identically.
    assert state.ghost_work_queue is not None
    candidates = build_research_interest_candidates(
        state.knowledge_store,
        session_id=session_id,
        project=project,
    )
    if candidates:
        state.ghost_work_queue.sync_from_sources(
            research_interest_candidates=candidates,
            session_id=session_id,
            run_id=f"seed-{case.name}",
        )


def _seed_prior_claim(
    state: server.State,
    *,
    session_id: str,
    project: str,
) -> None:
    url = "https://example.com/continuity-prior-claim"
    claim = "Provider recovery depends on a warm browser session."
    source_text = f"{claim} 2026 source note."
    summary = (
        "## 结论\n"
        f"- {claim} [1]\n\n"
        "## 关键证据\n"
        f"- [1] The opened source says {claim}\n\n"
        "## 反证与限制\n"
        "- 未找到强反证；需要持续追踪新的证据。\n\n"
        "## 来源质量\n"
        "- [1] secondary · web · fresh · example.com\n\n"
        "## 搜索覆盖\n"
        "- query: provider recovery\n"
        "- opened: Prior claim article\n"
        "- skipped: none representative\n\n"
        "## 来源\n"
        f"[1] Prior claim article - {url}"
    )
    ledger = ResearchLedger()
    ledger.record_search("provider recovery", [{
        "title": "Prior claim article",
        "url": url,
        "snippet": claim,
    }])
    ledger.record_open(
        requested_url=url,
        final_url=url,
        title="Prior claim article",
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
    ledger.add_evidence_items(list(prepared.items), note_id="prior-claim-note")
    quality = review_report_quality(
        summary,
        ledger=ledger,
        opened_sources=ledger.final_url_set(),
        search_result_urls={url},
    )
    if not quality.ok:
        raise AssertionError(quality.message)
    record = build_research_record(
        question="Research provider recovery",
        summary=summary,
        ledger=ledger,
        review=quality,
        run_id=f"run-{session_id}",
        session_id=session_id,
        project=project,
        synthesis_id=f"synthesis-{session_id}",
        stop_reason="done",
    )
    assert state.evidence_ledgers is not None
    state.evidence_ledgers.append_record(
        record,
        run_id=f"run-{session_id}",
        session_id=session_id,
        project=project,
    )


def _proof_record(question: str, *, run_id: str):
    url = "https://example.com/continuity-proof"
    claim = "Provider recovery should be re-checked against opened evidence."
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


def _payload(provider_id: str, rows: list[dict[str, Any]], *, complete: bool) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for arm in ARMS:
        arm_rows = [row for row in rows if row.get("arm") == arm]
        summary[arm] = {
            "total": len(arm_rows),
            "exact": sum(1 for row in arm_rows if row.get("exact")),
            "admitted": sum(1 for row in arm_rows if row.get("admitted")),
            "internal_leaks": sum(1 for row in arm_rows if row.get("internal_leak")),
            "errors": sum(1 for row in arm_rows if row.get("error")),
        }
    continuity_rows = [row for row in rows if row.get("arm") == "continuity"]
    summary["attribution"] = {
        "prior_claims_flagged": sum(
            1 for row in continuity_rows if row.get("prior_claim_flagged")
        ),
        "context_carried": sum(
            1 for row in continuity_rows if row.get("context_carried")
        ),
    }
    failure_classes: dict[str, int] = {}
    for row in rows:
        failure_class = row.get("failure_class")
        if failure_class and failure_class != "ok":
            failure_classes[failure_class] = failure_classes.get(failure_class, 0) + 1
    summary["failure_classes"] = failure_classes
    ok = (
        complete
        and summary["continuity"]["exact"] == summary["continuity"]["total"]
        and summary["baseline"]["exact"] == summary["baseline"]["total"]
        and summary["continuity"]["internal_leaks"] == 0
        and all(row.get("digest_only_payload") for row in rows)
    )
    return {
        "probe": "ghost_research_continuity_ab",
        "provider": provider_id,
        "complete": complete,
        "ok": ok,
        "summary": summary,
        "rows": rows,
    }


def _write_progress(path: Path | None, provider_id: str, rows: list[dict[str, Any]], *, complete: bool) -> None:
    if path is None:
        return
    _atomic_write_json(path, _payload(provider_id, rows, complete=complete))


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
    temporary.replace(path)


def _selected_cases(names: list[str]) -> tuple[ContinuityCase, ...]:
    if not names:
        return DEFAULT_CASES
    wanted = set(names)
    selected = tuple(case for case in DEFAULT_CASES if case.name in wanted)
    missing = sorted(wanted - {case.name for case in selected})
    if missing:
        raise SystemExit(f"unknown case(s): {', '.join(missing)}")
    return selected


_TRANSCRIPT_MODE_MAP = {
    "digest-only": TRANSCRIPT_MODE_DIGEST_ONLY,
    "archive": TRANSCRIPT_MODE_ARCHIVE,
}


def _open_journal(
    *,
    output: Path,
    provider_id: str,
    stamp: str,
    transcript_mode: str,
    max_turns: int,
    case_names: list[str],
) -> ABJournalWriter | None:
    """Open the manual journal for a live run (None when mode is off)."""
    mode = _TRANSCRIPT_MODE_MAP.get(str(transcript_mode or "").strip())
    if mode is None:
        # "off": no journal, no transcript material, no journal directory.
        return None
    journal_dir = output.parent / f"{output.stem}-journal"
    journal = ABJournalWriter(
        directory=journal_dir,
        experiment_id="ghost_research_continuity_ab",
        run_id=f"{provider_id}-{stamp}",
        provider=provider_id,
        # digest-only keeps hashes; archive stores full prompt/reply pairs
        # under transcripts/<digest>.json. Both stay manual-layer material.
        transcript_cache=TranscriptReplayCache(journal_dir, mode=mode),
    )
    journal.record_run_start(
        cases=tuple(case_names),
        arms=ARMS,
        max_turns=max(4, int(max_turns)),
    )
    return journal


def _self_test() -> None:
    payload = run_cases(provider_id="fake", provider_factory=None)
    if not payload["ok"]:
        raise AssertionError(json.dumps(payload["summary"], ensure_ascii=False))
    flagged = [
        row for row in payload["rows"]
        if row["case"] == "old-claim-must-be-rechecked"
        and row["arm"] == "continuity"
    ]
    if not flagged or not flagged[0].get("prior_claim_flagged"):
        raise AssertionError("old claim was not carried as a stale ref")
    assert classify_outcome(
        sends=1, replies=0, send_error_text="", stop_reason=""
    ) == "native_search_stall_suspected"
    assert classify_outcome(
        sends=0, replies=0, send_error_text="ConnectionError: x", stop_reason=""
    ) == "provider_send_error"
    assert classify_outcome(
        sends=1, replies=0, send_error_text="TimeoutError: send", stop_reason=""
    ) == "native_search_stall_suspected"
    assert classify_outcome(
        sends=3, replies=3, send_error_text="", stop_reason="max_turns"
    ) == "planner_quality:max_turns"
    print("self-test ok")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ghost Research Continuity A/B (narrow)")
    parser.add_argument("--provider", choices=("fake", *LIVE_PROVIDERS), default="fake")
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--max-turns", type=int, default=8)
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument(
        "--transcript-mode",
        choices=TRANSCRIPT_MODES,
        default="digest-only",
        help="digest-only keeps hashes (default, minimal retention); archive "
             "additionally stores full prompt/reply transcripts for offline "
             "prompt-lab diagnosis; off disables journaling entirely "
             "(manual layer only, never release evidence)",
    )
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)

    if args.self_test:
        _self_test()
        return 0

    cases = _selected_cases(args.case)
    if args.provider == "fake":
        # Offline stubs produce no transcript-worthy traffic: journaling
        # stays off regardless of --transcript-mode.
        payload = run_cases(provider_id="fake", cases=cases)
        output = args.output or RESULTS_DIR / "ghost_research_continuity_ab_offline.json"
        _atomic_write_json(output, payload)
    else:
        stamp = time.strftime("%Y%m%dT%H%M%S")
        output = args.output or RESULTS_DIR / (
            f"ghost_research_continuity_live_{args.provider}_{stamp}.json"
        )
        journal = _open_journal(
            output=output,
            provider_id=str(args.provider),
            stamp=stamp,
            transcript_mode=str(args.transcript_mode),
            max_turns=max(4, int(args.max_turns)),
            case_names=[case.name for case in cases],
        )
        try:
            payload = run_cases(
                provider_id=str(args.provider),
                cases=cases,
                provider_factory=lambda _label: connect_fresh_provider_tab(
                    args.provider, port=args.port
                ),
                live=True,
                max_turns=max(4, int(args.max_turns)),
                journal=journal,
            )
        finally:
            if journal is not None:
                journal.close()
        _atomic_write_json(output, payload)
    print(json.dumps({"ok": bool(payload.get("ok")), "output": str(output)}, ensure_ascii=False))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
