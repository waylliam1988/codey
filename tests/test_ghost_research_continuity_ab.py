from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import mock

from codey.agent import RunResult
from codey.providers.registry import DEFAULT_PROVIDER_ID
from codey.task_runner import TaskRequest, TaskRunner

from tests.manual import ghost_research_continuity_ab as ab
from tests.manual.ab_journal import (
    TRANSCRIPT_MODE_ARCHIVE,
    TRANSCRIPT_MODE_DIGEST_ONLY,
    TranscriptReplayCache,
)


def test_continuity_arm_admits_bounded_hints_and_baseline_stays_empty() -> None:
    payload = ab.run_cases(provider_id="fake", provider_factory=None)

    assert payload["ok"], json.dumps(payload, ensure_ascii=False, indent=2)
    summary = payload["summary"]
    assert summary["continuity"]["exact"] == summary["continuity"]["total"]
    assert summary["baseline"]["exact"] == summary["baseline"]["total"]
    # Baseline arm keeps the profile gate closed: nothing admitted anywhere.
    assert summary["baseline"]["admitted"] == 0
    assert summary["continuity"]["admitted"] >= 3
    assert summary["attribution"]["prior_claims_flagged"] == 1
    assert summary["continuity"]["internal_leaks"] == 0


def test_old_claim_is_carried_as_a_permanently_stale_ref() -> None:
    case = next(
        item for item in ab.DEFAULT_CASES
        if item.name == "old-claim-must-be-rechecked"
    )

    row = ab._run_case(case, arm="continuity", provider_factory=None)

    assert row["exact"]
    assert row["prior_claim_flagged"]
    assert row["digest_only_payload"]


def test_run_case_uses_selected_provider_identity() -> None:
    case = next(
        item for item in ab.DEFAULT_CASES
        if item.name == "empty-state-stays-baseline"
    )

    row = ab._run_case(
        case,
        arm="baseline",
        provider_id="qwen",
        provider_factory=None,
    )

    assert row["exact"]
    assert row["requested_provider"] == "qwen"
    assert row["task_provider"] == "qwen"
    assert row["observed_provider"] == "qwen"


def test_offline_fake_provider_uses_production_task_provider() -> None:
    case = next(
        item for item in ab.DEFAULT_CASES
        if item.name == "empty-state-stays-baseline"
    )

    row = ab._run_case(
        case,
        arm="baseline",
        provider_id="fake",
        provider_factory=None,
    )

    assert row["exact"], row
    assert row["requested_provider"] == "fake"
    assert row["task_provider"] == DEFAULT_PROVIDER_ID
    assert row["observed_provider"] == DEFAULT_PROVIDER_ID


def test_open_journal_off_mode_returns_none_without_side_effects() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        output = root / "live.json"

        journal = ab._open_journal(
            output=output,
            provider_id="deepseek",
            stamp="20260101T000000",
            transcript_mode="off",
            max_turns=8,
            case_names=["case-a"],
        )

        assert journal is None
        # No journal directory is created for an explicitly-off run.
        assert list(root.rglob("*-journal")) == []


def test_failure_classification_separates_provider_and_planner_causes() -> None:
    assert ab.classify_outcome(
        sends=2, replies=2, send_error_text="", stop_reason="done"
    ) == "ok"
    assert ab.classify_outcome(
        sends=1, replies=0, send_error_text="TimeoutError: send", stop_reason=""
    ) == "native_search_stall_suspected"
    assert ab.classify_outcome(
        sends=1, replies=0, send_error_text="", stop_reason=""
    ) == "native_search_stall_suspected"
    assert ab.classify_outcome(
        sends=0, replies=0, send_error_text="ConnectionError", stop_reason=""
    ) == "provider_send_error"
    assert ab.classify_outcome(
        sends=4, replies=4, send_error_text="", stop_reason="no_progress"
    ) == "planner_quality:no_progress"


def test_tracing_provider_journals_sends_and_archives_transcripts() -> None:
    """TracingProvider + ABJournalWriter capture every provider exchange."""
    from codey import server
    from codey.knowledge.store import KnowledgeStore

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        state = server.State(root / "state")
        state.knowledge_store = KnowledgeStore(root / "knowledge")
        journal_dir = root / "journal-archive"
        journal = ab.ABJournalWriter(
            directory=journal_dir,
            experiment_id="ghost_research_continuity_ab",
            run_id="fake-test-archive",
            provider="fake",
            transcript_cache=TranscriptReplayCache(
                journal_dir, mode=TRANSCRIPT_MODE_ARCHIVE
            ),
        )
        raw_provider = ab._MainProvider()
        tracing = ab.TracingProvider(raw_provider, journal=journal, case="c1", arm="baseline")
        runner = TaskRunner(
            state,
            agent_run=mock.Mock(return_value=RunResult("stub", "done", 1)),
            collect_changes=lambda *_a, **_k: {},
            run_review=mock.Mock(return_value=None),
            capture_provider_failure=server.capture_provider_failure,
            project_facts=state.project_facts,
            work_checkpoints=state.work_checkpoints,
            run_ledgers=state.run_ledgers,
            run_traces=state.run_traces,
            evidence_ledgers=state.evidence_ledgers,
            managed_outputs=state.managed_outputs,
            knowledge_store=state.knowledge_store,
            is_git_repository=lambda _p: True,
            ghost_router_provider_factory=None,
        )
        try:
            with mock.patch.object(state, "get_provider", return_value=tracing):
                runner.run(TaskRequest(
                    session_id="s-journal",
                    project=None,
                    task="hello",
                    max_turns=8,
                    continue_task=False,
                    provider_id="deepseek",
                    intent="auto",
                ))
        finally:
            journal.close()

        assert tracing.send_index == 1
        assert tracing.reply_count == 1
        event_types = [
            json.loads(line).get("event_type")
            for line in (journal_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert "send_start" in event_types
        assert "reply" in event_types
        transcripts = list((journal_dir / "transcripts").glob("*.json"))
        assert transcripts, "archive mode must store full prompt/reply transcripts"


def test_digest_only_journal_keeps_no_transcript_files() -> None:
    from codey import server
    from codey.knowledge.store import KnowledgeStore

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        state = server.State(root / "state")
        state.knowledge_store = KnowledgeStore(root / "knowledge")
        journal_dir = root / "journal-digest"
        journal = ab.ABJournalWriter(
            directory=journal_dir,
            experiment_id="ghost_research_continuity_ab",
            run_id="fake-test-digest",
            provider="fake",
            transcript_cache=TranscriptReplayCache(
                journal_dir, mode=TRANSCRIPT_MODE_DIGEST_ONLY
            ),
        )
        raw_provider = ab._MainProvider()
        tracing = ab.TracingProvider(raw_provider, journal=journal, case="c1", arm="baseline")
        runner = TaskRunner(
            state,
            agent_run=mock.Mock(return_value=RunResult("stub", "done", 1)),
            collect_changes=lambda *_a, **_k: {},
            run_review=mock.Mock(return_value=None),
            capture_provider_failure=server.capture_provider_failure,
            project_facts=state.project_facts,
            work_checkpoints=state.work_checkpoints,
            run_ledgers=state.run_ledgers,
            run_traces=state.run_traces,
            evidence_ledgers=state.evidence_ledgers,
            managed_outputs=state.managed_outputs,
            knowledge_store=state.knowledge_store,
            is_git_repository=lambda _p: True,
            ghost_router_provider_factory=None,
        )
        try:
            with mock.patch.object(state, "get_provider", return_value=tracing):
                runner.run(TaskRequest(
                    session_id="s-journal-digest",
                    project=None,
                    task="hello",
                    max_turns=8,
                    continue_task=False,
                    provider_id="deepseek",
                    intent="auto",
                ))
        finally:
            journal.close()

        assert tracing.send_index == 1
        assert tracing.reply_count == 1
        assert list((journal_dir / "transcripts").glob("*.json")) == []


def test_run_cases_marks_live_journal_complete() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        journal_dir = root / "journal-complete"
        journal = ab.ABJournalWriter(
            directory=journal_dir,
            experiment_id="ghost_research_continuity_ab",
            run_id="fake-test-complete",
            provider="fake",
            transcript_cache=TranscriptReplayCache(
                journal_dir, mode=TRANSCRIPT_MODE_DIGEST_ONLY
            ),
        )
        try:
            payload = ab.run_cases(
                provider_id="fake",
                cases=(ab.DEFAULT_CASES[0],),
                provider_factory=None,
                journal=journal,
            )
        finally:
            journal.close()

        assert payload["ok"]
        manifest = json.loads(
            (journal_dir / "manifest.json").read_text(encoding="utf-8")
        )
        assert manifest["status"] == "done"
        event_types = [
            json.loads(line).get("event_type")
            for line in (journal_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert event_types[-1] == "run_complete"
