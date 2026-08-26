from __future__ import annotations

import json
from pathlib import Path
import tempfile
from unittest import mock

from codey.app import server
from codey.agents.handoff import ConversationContext, ConversationSnapshot
from codey.knowledge.note import KnowledgeNote
from codey.knowledge.store import KnowledgeStore
from codey.research.context import ResearchContext
from codey.research.ledger import ResearchLedger
from codey.research.object_model import build_research_record
from codey.research.report_quality import review_report_quality
from codey.app.task_runner import (
    TaskRequest,
    TaskRunner,
    _RunFrame,
)

_QUESTION = "Should provider recovery be re-checked against fresh sources?"


def _make_frame(project_text: str) -> _RunFrame:
    request = TaskRequest(
        session_id="s1",
        project=None,
        task="research the continuity question",
        max_turns=8,
        continue_task=False,
        provider_id="deepseek",
    )
    return _RunFrame(
        request=request,
        run_id="run-topic-continuity",
        task_kind="research",
        provider=None,
        provider_id="deepseek",
        project_text=project_text,
        conversation=ConversationContext(),
        fresh_chat=True,
        handoff="",
        research_handoff="",
        prior_snapshot=ConversationSnapshot(mode="research"),
        recovered_owner_prompt="",
        provider_session_changed=False,
        preflight_tried=set(),
        preflight_switches=0,
    )


def _runner(state: server.State) -> TaskRunner:
    return TaskRunner(
        state,
        agent_run=mock.Mock(),
        collect_changes=lambda *_args, **_kwargs: {},
        run_review=mock.Mock(return_value=None),
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


def _seed_open_question_note(store: KnowledgeStore, *, session_id: str) -> None:
    store.write_note(KnowledgeNote.create(
        id=f"synthesis-continuity-{session_id}",
        type="synthesis",
        title="Provider recovery synthesis",
        body="Research synthesis with an open follow-up question.",
        open_questions=[_QUESTION],
        tags=["research"],
        session_id=session_id,
    ))


def _seed_prior_claim(state: server.State, *, session_id: str, project: str) -> None:
    url = "https://example.com/prior-claim"
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
    assert not prepared.error
    ledger.add_evidence_items(list(prepared.items), note_id="prior-claim-note")
    quality = review_report_quality(
        summary,
        ledger=ledger,
        opened_sources=ledger.final_url_set(),
        search_result_urls={url},
    )
    assert quality.ok
    record = build_research_record(
        question="Research provider recovery",
        summary=summary,
        ledger=ledger,
        review=quality,
        run_id="run-prior-claim",
        session_id=session_id,
        project=project,
        synthesis_id="synthesis-prior-claim",
        stop_reason="done",
    )
    assert state.evidence_ledgers is not None
    state.evidence_ledgers.append_record(
        record,
        run_id="run-prior-claim",
        session_id=session_id,
        project=project,
    )


def test_topic_continuity_admission_is_bounded_refs_and_hint_text() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        state = server.State(root / "state")
        state.knowledge_store = KnowledgeStore(root / "knowledge")
        _seed_open_question_note(state.knowledge_store, session_id="s1")
        if state.ghost_continuity is not None:
            state.ghost_continuity.sync_from_sources(
                knowledge_store=state.knowledge_store,
                session_id="s1",
                run_id="run-seed",
            )
        runner = _runner(state)

        text, payload = runner._build_research_topic_continuity(
            session_id="s1",
            project=str(root / "project"),
        )

    assert text, "expected admitted continuity text"
    assert "not evidence" in text
    assert _QUESTION in text
    lowered = text.casefold()
    assert not any(term in lowered for term in ("ghost", "work queue", "concept graph"))
    assert payload is not None and payload["admitted"] is True
    raw = json.dumps(payload, ensure_ascii=False)
    assert _QUESTION not in raw  # digest-only trace projection
    refs = [
        ref
        for row in payload["items"]
        for ref in row["refs"]
    ]
    assert any(ref.startswith("continuity:") or ref.startswith("research_interest:") for ref in refs)


def _seed_prior_claim_overflow(
    state: server.State,
    *,
    session_id: str,
    project: str,
    count: int,
) -> None:
    """Seed one ledger record carrying `count` distinct evidence claims."""
    url = "https://example.com/prior-claim-overflow"
    sentences = [
        f"Overflow claim number {index} needs a fresh source check."
        for index in range(count)
    ]
    source_text = " ".join(sentences)
    excerpt_lines = "\n".join(
        f"- [1] {sentence}" for sentence in sentences
    )
    summary = (
        "## 结论\n"
        "- Overflow claims recorded. [1]\n\n"
        "## 关键证据\n"
        f"{excerpt_lines}\n\n"
        "## 反证与限制\n"
        "- 未找到强反证；需要持续追踪新的证据。\n\n"
        "## 来源质量\n"
        "- [1] secondary · web · fresh · example.com\n\n"
        "## 搜索覆盖\n"
        "- query: overflow\n"
        "- opened: Overflow article\n"
        "- skipped: none representative\n\n"
        "## 来源\n"
        f"[1] Overflow article - {url}"
    )
    ledger = ResearchLedger()
    ledger.record_open(
        requested_url=url,
        final_url=url,
        title="Overflow article",
        text=source_text,
    )
    prepared = ledger.prepare_evidence_items(
        [
            {
                "claim": sentence,
                "source_url": url,
                "excerpt": sentence,
                "stance": "supports",
            }
            for sentence in sentences
        ],
        fallback_sources=[url],
        fallback_claim=sentences[0],
        fallback_body=source_text[:200],
        note_type="fact",
    )
    assert not prepared.error and len(prepared.items) == count
    ledger.add_evidence_items(list(prepared.items), note_id="overflow-note")
    quality = review_report_quality(
        summary,
        ledger=ledger,
        opened_sources=ledger.final_url_set(),
        search_result_urls={url},
    )
    if not quality.ok:
        raise AssertionError(quality.message)
    record = build_research_record(
        question="Research overflow",
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


def test_prior_claim_overflow_reports_truncated_honestly() -> None:
    from codey.research.topic_continuity import MAX_TOPIC_CLAIM_REFS

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        project = str(root / "project")
        (root / "project").mkdir()
        state = server.State(root / "state")
        state.knowledge_store = KnowledgeStore(root / "knowledge")
        _seed_prior_claim_overflow(
            state,
            session_id="s1",
            project=project,
            count=MAX_TOPIC_CLAIM_REFS + 2,
        )
        runner = _runner(state)

        refs = runner._prior_claim_refs(session_id="s1", project=project)
        _text, payload = runner._build_research_topic_continuity(
            session_id="s1",
            project=project,
        )

    # The extraction keeps the overflow signal (MAX+1) so the projection's
    # truncated verdict is honest even though production caps the carried
    # refs before projecting.
    assert len(refs) == MAX_TOPIC_CLAIM_REFS + 1
    assert payload is not None
    assert payload["truncated"] is True
    assert payload["claim_ref_count"] == MAX_TOPIC_CLAIM_REFS


def test_prior_claims_enter_as_stale_refs_never_evidence() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        project = str(root / "project")
        (root / "project").mkdir()
        state = server.State(root / "state")
        state.knowledge_store = KnowledgeStore(root / "knowledge")
        _seed_prior_claim(state, session_id="s1", project=project)
        runner = _runner(state)

        text, payload = runner._build_research_topic_continuity(
            session_id="s1",
            project=project,
        )

    prior_rows = [
        row for row in (payload or {}).get("items", [])
        if row["kind"] == "prior_claim"
    ]
    assert prior_rows, "prior ledger claims must be carried as refs"
    assert all(row["stale"] is True for row in prior_rows)
    assert all("prior_claim_needs_recheck" in row["reason_codes"] for row in prior_rows)
    raw = json.dumps(payload or {})
    assert "evidence_refs" not in raw
    assert "re-check" in text


def test_closed_profile_gate_returns_empty_baseline() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        state = server.State(root / "state")
        state.knowledge_store = KnowledgeStore(root / "knowledge")
        _seed_open_question_note(state.knowledge_store, session_id="s1")
        runner = _runner(state)

        with mock.patch(
            "codey.app.task_runner.allows_context_source",
            return_value=False,
        ):
            text, payload = runner._build_research_topic_continuity(
                session_id="s1",
                project=str(root / "project"),
            )

    assert text == ""
    assert payload is None


def test_cancellation_is_never_swallowed_by_the_builder() -> None:
    # Stop/cancel semantics must not depend on luck: the fail-open guard
    # covers projection failures only.
    from codey.runtime import cancellation

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        state = server.State(root / "state")
        state.knowledge_store = KnowledgeStore(root / "knowledge")
        _seed_open_question_note(state.knowledge_store, session_id="s1")
        runner = _runner(state)
        trace_calls: list[tuple] = []

        with mock.patch.object(
            runner,
            "_prior_claim_refs",
            side_effect=cancellation.TaskCancelled("task stopped"),
        ):
            try:
                runner._build_research_topic_continuity(
                    session_id="s1",
                    project=str(root / "project"),
                    trace=lambda *a, **k: trace_calls.append((a, k)),
                )
            except cancellation.TaskCancelled:
                pass
            else:
                raise AssertionError("TaskCancelled was swallowed by the builder")

    assert trace_calls == []  # no warn row: this is a stop, not a failure


def test_empty_local_state_admits_nothing() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        state = server.State(root / "state")
        state.knowledge_store = KnowledgeStore(root / "knowledge")
        runner = _runner(state)

        text, payload = runner._build_research_topic_continuity(
            session_id="s1",
            project=str(root / "project"),
        )

    assert text == ""
    assert payload is not None
    assert payload["admitted"] is False


def test_build_research_context_carries_bounded_continuity() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        project = str(root / "project")
        (root / "project").mkdir()
        state = server.State(root / "state")
        state.knowledge_store = KnowledgeStore(root / "knowledge")
        _seed_open_question_note(state.knowledge_store, session_id="s1")
        if state.ghost_continuity is not None:
            state.ghost_continuity.sync_from_sources(
                knowledge_store=state.knowledge_store,
                session_id="s1",
                run_id="run-seed-context",
            )
        runner = _runner(state)
        frame = _make_frame(project_text=project)

        context = runner._build_research_context(
            frame,
            frame.request,
            proof_question="proof",
            max_turns=8,
        )

    assert isinstance(context, ResearchContext)
    assert context.topic_continuity_context
    assert "not evidence" in context.topic_continuity_context
    payload = context.topic_continuity_payload or {}
    assert payload.get("admitted") is True
    assert context.question == "research the continuity question"


def test_build_research_context_baseline_without_seeds() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        state = server.State(root / "state")
        state.knowledge_store = KnowledgeStore(root / "knowledge")
        runner = _runner(state)
        frame = _make_frame(project_text=str(root / "project"))

        context = runner._build_research_context(
            frame,
            frame.request,
            proof_question="",
            max_turns=6,
        )

    assert context.topic_continuity_context == ""
    assert context.topic_continuity_payload is not None
    assert context.topic_continuity_payload["admitted"] is False